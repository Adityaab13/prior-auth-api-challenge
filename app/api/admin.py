"""Operations Admin persona router: full visibility, reassignment, audit trail, workload, support."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.errors import COMMON_ERROR_RESPONSES
from app.auth import AuthenticatedUser, require_roles
from app.db.session import get_db_session
from app.domain.types import (
    AuditEventType,
    AuditRecord,
    CodeType,
    PriorAuthRequest,
    ProcedureDecision,
    RequestStatus,
    ReviewerWorkload,
    SubmissionAttemptRecord,
    UserRole,
)
from app.logging_config import request_id_ctx
from app.services import query_service, review_service

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Operations Admin"],
    responses={401: COMMON_ERROR_RESPONSES[401], 403: COMMON_ERROR_RESPONSES[403]},
)

require_admin = require_roles(UserRole.ADMIN)


class AdminProcedureView(BaseModel):
    id: str
    code_type: CodeType
    code: str
    units: int
    high_units_confirmed: bool
    decision: ProcedureDecision
    denial_reason: str | None


class AuditEventView(BaseModel):
    id: str
    event_type: AuditEventType
    actor_id: str
    actor_role: UserRole
    occurred_at: datetime
    correlation_id: str
    metadata: dict[str, Any]


class AdminRequestSummaryView(BaseModel):
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
    procedures: list[AdminProcedureView]


class AdminRequestDetailView(AdminRequestSummaryView):
    audit_events: list[AuditEventView]


class ReassignPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "reviewer_id": "user-rev-nurse",
                    "reason": "Balancing daily nurse review queue.",
                }
            ]
        }
    )

    reviewer_id: str = Field(..., description="Target reviewer user ID.")
    reason: str | None = Field(
        default=None, description="Optional non-PHI operational reason for reassignment."
    )


class ReviewerWorkloadView(BaseModel):
    reviewer_id: str
    display_name: str
    open_request_count: int


class SubmissionAttemptView(BaseModel):
    id: str
    correlation_id: str
    practice_id: str
    actor_id: str
    occurred_at: datetime
    outcome: str
    error_codes: list[str]
    prior_auth_id: str | None


def _to_admin_summary(req: PriorAuthRequest) -> AdminRequestSummaryView:
    return AdminRequestSummaryView(
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
            AdminProcedureView(
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


def _to_audit_view(ev: AuditRecord) -> AuditEventView:
    return AuditEventView(
        id=ev.id,
        event_type=ev.event_type,
        actor_id=ev.actor_id,
        actor_role=ev.actor_role,
        occurred_at=ev.occurred_at,
        correlation_id=ev.correlation_id,
        metadata=ev.metadata,
    )


@router.get(
    "/requests",
    response_model=list[AdminRequestSummaryView],
    summary="List all prior authorization requests across practices",
)
def list_all_requests(
    status_filter: RequestStatus | None = Query(default=None, alias="status"),
    practice_id: str | None = Query(default=None),
    assigned_reviewer_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: AuthenticatedUser = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> list[AdminRequestSummaryView]:
    items = query_service.list_admin_requests(
        session,
        status=status_filter,
        practice_id=practice_id,
        assigned_reviewer_id=assigned_reviewer_id,
        limit=limit,
        offset=offset,
    )
    return [_to_admin_summary(item) for item in items]


@router.get(
    "/requests/{request_id}",
    response_model=AdminRequestDetailView,
    summary="Get full request details and append-only audit trail",
    responses={404: COMMON_ERROR_RESPONSES[404]},
)
def get_request_with_audit(
    request_id: str,
    _admin: AuthenticatedUser = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> AdminRequestDetailView:
    req, events = query_service.get_admin_request_with_audit(session, request_id=request_id)
    summary = _to_admin_summary(req)
    return AdminRequestDetailView(
        **summary.model_dump(),
        audit_events=[_to_audit_view(e) for e in events],
    )


@router.post(
    "/requests/{request_id}/reassign",
    response_model=AdminRequestSummaryView,
    summary="Reassign an open request to another reviewer",
    description=(
        "Reassigns a submitted or in-review request to another reviewer while enforcing "
        "conflict-of-interest checks and recording an immutable audit event."
    ),
    responses={
        404: COMMON_ERROR_RESPONSES[404],
        409: COMMON_ERROR_RESPONSES[409],
        422: COMMON_ERROR_RESPONSES[422],
    },
)
def reassign_request(
    request_id: str,
    payload: ReassignPayload,
    admin: AuthenticatedUser = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> AdminRequestSummaryView:
    updated = review_service.reassign_request(
        session,
        admin_user=admin,
        request_id=request_id,
        target_reviewer_id=payload.reviewer_id,
        reason=payload.reason,
        correlation_id=request_id_ctx.get(),
    )
    return _to_admin_summary(updated)


@router.get(
    "/reviewers/workload",
    response_model=list[ReviewerWorkloadView],
    summary="View open request counts per reviewer",
    description="Shows how many open (`in_review`) requests each registered reviewer currently holds.",
)
def reviewer_workload(
    _admin: AuthenticatedUser = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> list[ReviewerWorkloadView]:
    workloads: list[ReviewerWorkload] = query_service.get_reviewer_workloads(session)
    return [
        ReviewerWorkloadView(
            reviewer_id=w.reviewer_id,
            display_name=w.display_name,
            open_request_count=w.open_request_count,
        )
        for w in workloads
    ]


@router.get(
    "/support/submissions",
    response_model=list[SubmissionAttemptView],
    summary="Support desk lookup for submission attempts (PHI-free)",
    description=(
        "Allows operations and support teams to investigate 'my submission didn't go through' "
        "calls using `request_id` (`X-Request-ID`) or `practice_id`, without exposing PHI."
    ),
)
def support_submission_lookup(
    request_id: str | None = Query(
        default=None, description="Exact X-Request-ID correlation header returned to the office."
    ),
    practice_id: str | None = Query(default=None),
    outcome: str | None = Query(default=None, pattern="^(accepted|rejected)$"),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: AuthenticatedUser = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> list[SubmissionAttemptView]:
    attempts: list[SubmissionAttemptRecord] = query_service.list_support_submission_attempts(
        session,
        correlation_id=request_id,
        practice_id=practice_id,
        outcome=outcome,
        limit=limit,
    )
    return [
        SubmissionAttemptView(
            id=a.id,
            correlation_id=a.correlation_id,
            practice_id=a.practice_id,
            actor_id=a.actor_id,
            occurred_at=a.occurred_at,
            outcome=a.outcome,
            error_codes=list(a.error_codes),
            prior_auth_id=a.prior_auth_id,
        )
        for a in attempts
    ]
