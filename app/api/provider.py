"""Provider Office persona router and DTOs (/api/v1/provider)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.errors import COMMON_ERROR_RESPONSES
from app.auth import AuthenticatedUser, require_roles
from app.db.session import get_db_session
from app.domain.types import (
    CodeType,
    NotificationRecord,
    PriorAuthRequest,
    ProcedureDecision,
    ProcedureInput,
    RequestStatus,
    SubmissionCommand,
    UserRole,
)
from app.logging_config import request_id_ctx
from app.services import query_service, submission_service

router = APIRouter(
    prefix="/api/v1/provider",
    tags=["Provider Office"],
    responses={401: COMMON_ERROR_RESPONSES[401], 403: COMMON_ERROR_RESPONSES[403]},
)

require_provider = require_roles(UserRole.PROVIDER_OFFICE)


class ProcedureSubmissionSchema(BaseModel):
    code_type: CodeType = Field(..., description="Procedure coding system (`CPT` or `HCPCS`).")
    code: str = Field(..., description="CPT (e.g. `72148`) or HCPCS Level II (e.g. `J1745`) code.")
    units: int = Field(
        ...,
        description="Requested units (1–999). Values > 30 require `confirm_high_units=true`.",
    )
    confirm_high_units: bool = Field(
        default=False,
        description="Set to `true` to confirm units > 30 are intentional and not a typo.",
    )


class SubmitPriorAuthPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "member_id": "mem-alice",
                    "diagnosis_code": "M54.50",
                    "date_of_service": "2026-12-15",
                    "procedures": [
                        {
                            "code_type": "CPT",
                            "code": "72148",
                            "units": 1,
                            "confirm_high_units": False,
                        },
                        {
                            "code_type": "CPT",
                            "code": "97110",
                            "units": 12,
                            "confirm_high_units": False,
                        },
                    ],
                    "clinical_note": "Patient reports 8 weeks of persistent lumbar radiculopathy.",
                }
            ]
        }
    )

    member_id: str = Field(..., description="Oxbridge member ID linked to your practice.")
    diagnosis_code: str = Field(..., description="Primary ICD-10-CM diagnosis code.")
    date_of_service: date = Field(
        ...,
        description=(
            "Planned date of service (YYYY-MM-DD). Evaluated in your practice's local timezone; "
            "must be today or within 1 year."
        ),
    )
    procedures: list[ProcedureSubmissionSchema] = Field(
        ..., description="One or more requested CPT/HCPCS procedures."
    )
    clinical_note: str | None = Field(
        default=None,
        description="Optional supporting clinical narrative (never shown to members).",
    )


class ProviderProcedureView(BaseModel):
    id: str
    code_type: CodeType
    code: str
    units: int
    high_units_confirmed: bool
    decision: ProcedureDecision
    denial_reason: str | None = Field(
        default=None, description="Populated when a procedure is denied."
    )


class ProviderRequestView(BaseModel):
    id: str
    member_id: str
    practice_id: str
    diagnosis_code: str
    date_of_service: date
    clinical_note: str | None
    status: RequestStatus
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
    procedures: list[ProviderProcedureView]


class ProviderNotificationView(BaseModel):
    id: str
    request_id: str
    event_type: str
    status: str
    payload: dict[str, Any]
    created_at: datetime


def _to_provider_view(req: PriorAuthRequest) -> ProviderRequestView:
    return ProviderRequestView(
        id=req.id,
        member_id=req.member_id,
        practice_id=req.practice_id,
        diagnosis_code=req.diagnosis_code,
        date_of_service=req.date_of_service,
        clinical_note=req.clinical_note,
        status=req.status,
        decided_at=req.decided_at,
        created_at=req.created_at,
        updated_at=req.updated_at,
        procedures=[
            ProviderProcedureView(
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


def _to_notification_view(n: NotificationRecord) -> ProviderNotificationView:
    return ProviderNotificationView(
        id=n.id,
        request_id=n.request_id,
        event_type=n.event_type,
        status=n.status,
        payload=n.payload,
        created_at=n.created_at,
    )


@router.post(
    "/requests",
    response_model=ProviderRequestView,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a prior authorization request",
    description=(
        "Submits a new prior authorization request for a patient linked to the caller's practice. "
        "All validation rules (patient link, practice-local date window, active coverage, ICD-10 "
        "and CPT/HCPCS syntax, and >30-unit typo threshold) are evaluated together so any 422 "
        "response tells the office every field to fix in a single round-trip. Pass an "
        "`Idempotency-Key` header to safely retry network failures without creating duplicates."
    ),
    responses={422: COMMON_ERROR_RESPONSES[422]},
)
def submit_request(
    payload: SubmitPriorAuthPayload,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional client-generated key to make retries idempotent per practice.",
    ),
    user: AuthenticatedUser = Depends(require_provider),
    session: Session = Depends(get_db_session),
) -> ProviderRequestView:
    cmd = SubmissionCommand(
        member_id=payload.member_id,
        diagnosis_code=payload.diagnosis_code,
        date_of_service=payload.date_of_service,
        procedures=tuple(
            ProcedureInput(
                code_type=p.code_type,
                code=p.code,
                units=p.units,
                confirm_high_units=p.confirm_high_units,
            )
            for p in payload.procedures
        ),
        clinical_note=payload.clinical_note,
    )
    created, is_replay = submission_service.submit_prior_auth(
        session,
        user=user,
        cmd=cmd,
        correlation_id=request_id_ctx.get(),
        idempotency_key=idempotency_key,
    )
    if is_replay:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotency-Replayed"] = "true"
    return _to_provider_view(created)


@router.get(
    "/requests",
    response_model=list[ProviderRequestView],
    summary="List requests submitted by your practice",
)
def list_requests(
    status_filter: RequestStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(require_provider),
    session: Session = Depends(get_db_session),
) -> list[ProviderRequestView]:
    items = query_service.list_provider_requests(
        session, user=user, status=status_filter, limit=limit, offset=offset
    )
    return [_to_provider_view(item) for item in items]


@router.get(
    "/requests/{request_id}",
    response_model=ProviderRequestView,
    summary="Get a single request submitted by your practice",
    responses={404: COMMON_ERROR_RESPONSES[404]},
)
def get_request(
    request_id: str,
    user: AuthenticatedUser = Depends(require_provider),
    session: Session = Depends(get_db_session),
) -> ProviderRequestView:
    req = query_service.get_provider_request(session, user=user, request_id=request_id)
    return _to_provider_view(req)


@router.get(
    "/notifications",
    response_model=list[ProviderNotificationView],
    summary="List decision notifications for your practice",
    description=(
        "Returns decision notifications recorded transactionally when reviewers decide requests "
        "submitted by your practice."
    ),
)
def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(require_provider),
    session: Session = Depends(get_db_session),
) -> list[ProviderNotificationView]:
    notes = query_service.list_provider_notifications(session, user=user, limit=limit)
    return [_to_notification_view(n) for n in notes]
