"""Reviewer (nurse / physician) persona router and DTOs (/api/v1/reviewer)."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.errors import COMMON_ERROR_RESPONSES
from app.auth import AuthenticatedUser, require_roles
from app.db.session import get_db_session
from app.domain.types import (
    CodeType,
    PriorAuthRequest,
    ProcedureDecision,
    ProcedureDecisionInput,
    RequestStatus,
    UserRole,
)
from app.logging_config import request_id_ctx
from app.services import query_service, review_service

router = APIRouter(
    prefix="/api/v1/reviewer",
    tags=["Reviewer"],
    responses={401: COMMON_ERROR_RESPONSES[401], 403: COMMON_ERROR_RESPONSES[403]},
)

require_reviewer = require_roles(UserRole.REVIEWER)


class ReviewerProcedureView(BaseModel):
    id: str
    code_type: CodeType
    code: str
    units: int
    high_units_confirmed: bool
    decision: ProcedureDecision
    denial_reason: str | None


class ReviewerRequestView(BaseModel):
    id: str
    member_id: str
    practice_id: str
    submitted_by: str
    diagnosis_code: str
    date_of_service: date
    clinical_note: str | None
    status: RequestStatus
    assigned_reviewer_id: str | None
    assigned_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
    procedures: list[ReviewerProcedureView]


class ProcedureDecisionItemPayload(BaseModel):
    procedure_id: str = Field(..., description="ID of the procedure line within this request.")
    decision: ProcedureDecision = Field(..., description="Must be `approved` or `denied`.")
    denial_reason: str | None = Field(
        default=None,
        description="Required (non-empty) when `decision=denied`; must be omitted when approved.",
    )


class SubmitDecisionPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "decisions": [
                        {
                            "procedure_id": "00000000-0000-0000-0000-000000000001",
                            "decision": "approved",
                        },
                        {
                            "procedure_id": "00000000-0000-0000-0000-000000000002",
                            "decision": "denied",
                            "denial_reason": "Documentation of 6 weeks conservative therapy is missing.",
                        },
                    ]
                }
            ]
        }
    )

    decisions: list[ProcedureDecisionItemPayload] = Field(
        ...,
        description="Decisions for EVERY procedure in the request, submitted atomically.",
    )


def _to_reviewer_view(req: PriorAuthRequest) -> ReviewerRequestView:
    return ReviewerRequestView(
        id=req.id,
        member_id=req.member_id,
        practice_id=req.practice_id,
        submitted_by=req.submitted_by,
        diagnosis_code=req.diagnosis_code,
        date_of_service=req.date_of_service,
        clinical_note=req.clinical_note,
        status=req.status,
        assigned_reviewer_id=req.assigned_reviewer_id,
        assigned_at=req.assigned_at,
        decided_by=req.decided_by,
        decided_at=req.decided_at,
        created_at=req.created_at,
        updated_at=req.updated_at,
        procedures=[
            ReviewerProcedureView(
                id=p.id,
                code_type=p.code_type,
                code=p.code,
                units=p.units,
                high_units_confirmed=p.high_units_confirmed,
                decision=p.decision,
                denial_reason=p.denial_reason,
            )
            for p in req.procedures
        ],
    )


class QueueItemView(BaseModel):
    """Minimal queue metadata for unassigned requests (COI patients are excluded)."""

    id: str
    practice_id: str
    date_of_service: date
    procedure_count: int
    created_at: datetime


@router.get(
    "/queue",
    response_model=list[QueueItemView],
    summary="List eligible unassigned requests in the review queue",
    description=(
        "Returns unassigned requests in FIFO order, automatically excluding any request for a "
        "patient the caller treats (conflict-of-interest enforcement)."
    ),
)
def list_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(require_reviewer),
    session: Session = Depends(get_db_session),
) -> list[QueueItemView]:
    items = query_service.list_reviewer_queue(session, user=user, limit=limit, offset=offset)
    return [
        QueueItemView(
            id=item.id,
            practice_id=item.practice_id,
            date_of_service=item.date_of_service,
            procedure_count=len(item.procedures),
            created_at=item.created_at,
        )
        for item in items
    ]


@router.post(
    "/queue/claim-next",
    response_model=ReviewerRequestView,
    summary="Claim the next eligible request from the queue",
    description=(
        "Atomically assigns the oldest unassigned request to the caller using PostgreSQL "
        "`FOR UPDATE SKIP LOCKED`. Automatically skips requests for any patient the caller treats "
        "(conflict-of-interest enforcement)."
    ),
    responses={404: COMMON_ERROR_RESPONSES[404]},
)
def claim_next(
    user: AuthenticatedUser = Depends(require_reviewer),
    session: Session = Depends(get_db_session),
) -> ReviewerRequestView:
    claimed = review_service.claim_next_request(
        session, user=user, correlation_id=request_id_ctx.get()
    )
    return _to_reviewer_view(claimed)


@router.post(
    "/requests/{request_id}/claim",
    response_model=ReviewerRequestView,
    summary="Claim a specific unassigned request",
    responses={404: COMMON_ERROR_RESPONSES[404], 409: COMMON_ERROR_RESPONSES[409]},
)
def claim_specific(
    request_id: str,
    user: AuthenticatedUser = Depends(require_reviewer),
    session: Session = Depends(get_db_session),
) -> ReviewerRequestView:
    claimed = review_service.claim_specific_request(
        session, user=user, request_id=request_id, correlation_id=request_id_ctx.get()
    )
    return _to_reviewer_view(claimed)


@router.get(
    "/requests",
    response_model=list[ReviewerRequestView],
    summary="List requests assigned to you",
)
def list_assigned_requests(
    status_filter: RequestStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(require_reviewer),
    session: Session = Depends(get_db_session),
) -> list[ReviewerRequestView]:
    items = query_service.list_reviewer_requests(
        session, user=user, status=status_filter, limit=limit, offset=offset
    )
    return [_to_reviewer_view(item) for item in items]


@router.get(
    "/requests/{request_id}",
    response_model=ReviewerRequestView,
    summary="Get an assigned request for review",
    responses={404: COMMON_ERROR_RESPONSES[404]},
)
def get_assigned_request(
    request_id: str,
    user: AuthenticatedUser = Depends(require_reviewer),
    session: Session = Depends(get_db_session),
) -> ReviewerRequestView:
    req = query_service.get_reviewer_request(session, user=user, request_id=request_id)
    return _to_reviewer_view(req)


@router.post(
    "/requests/{request_id}/decision",
    response_model=ReviewerRequestView,
    summary="Submit final per-procedure decision for the whole request",
    description=(
        "Atomically approves or denies every procedure in the request, writes an immutable "
        "compliance audit event, and creates a decision notification for the provider office. "
        "Decisions are final and cannot be changed."
    ),
    responses={
        404: COMMON_ERROR_RESPONSES[404],
        409: COMMON_ERROR_RESPONSES[409],
        422: COMMON_ERROR_RESPONSES[422],
    },
)
def decide_request(
    request_id: str,
    payload: SubmitDecisionPayload,
    user: AuthenticatedUser = Depends(require_reviewer),
    session: Session = Depends(get_db_session),
) -> ReviewerRequestView:
    decision_inputs = tuple(
        ProcedureDecisionInput(
            procedure_id=d.procedure_id,
            decision=d.decision,
            denial_reason=d.denial_reason,
        )
        for d in payload.decisions
    )
    decided = review_service.submit_decision(
        session,
        user=user,
        request_id=request_id,
        decisions=decision_inputs,
        correlation_id=request_id_ctx.get(),
    )
    return _to_reviewer_view(decided)
