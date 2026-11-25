"""Member (patient) persona router and strictly PHI-scoped DTOs (/api/v1/member).

Clinical notes and reviewer reasoning / denial reasons do NOT exist on Member schemas,
structurally preventing accidental internal-data leakage to members.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.errors import COMMON_ERROR_RESPONSES
from app.auth import AuthenticatedUser, require_roles
from app.db.session import get_db_session
from app.domain.types import CodeType, PriorAuthRequest, ProcedureDecision, RequestStatus, UserRole
from app.services import query_service

router = APIRouter(
    prefix="/api/v1/member",
    tags=["Member"],
    responses={401: COMMON_ERROR_RESPONSES[401], 403: COMMON_ERROR_RESPONSES[403]},
)

require_member = require_roles(UserRole.MEMBER)


class MemberProcedureView(BaseModel):
    """Member-safe procedure view: exposes approval outcome only (no reviewer reasoning)."""

    id: str
    code_type: CodeType
    code: str
    units: int
    decision: ProcedureDecision = Field(
        ..., description="Per-procedure outcome (`pending`, `approved`, or `denied`)."
    )


class MemberRequestView(BaseModel):
    """Member-safe request view: strictly omits clinical notes and reviewer reasoning."""

    id: str
    date_of_service: date
    status: RequestStatus
    decided_at: datetime | None
    created_at: datetime
    procedures: list[MemberProcedureView]


def _to_member_view(req: PriorAuthRequest) -> MemberRequestView:
    return MemberRequestView(
        id=req.id,
        date_of_service=req.date_of_service,
        status=req.status,
        decided_at=req.decided_at,
        created_at=req.created_at,
        procedures=[
            MemberProcedureView(
                id=p.id,
                code_type=p.code_type,
                code=p.code,
                units=p.units,
                decision=p.decision,
            )
            for p in req.procedures
        ],
    )


@router.get(
    "/requests",
    response_model=list[MemberRequestView],
    summary="List your prior authorization requests",
    description=(
        "Returns prior authorization requests belonging to the authenticated member along with "
        "per-procedure approval status. Clinical notes and reviewer reasoning are never exposed."
    ),
)
def list_my_requests(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(require_member),
    session: Session = Depends(get_db_session),
) -> list[MemberRequestView]:
    items = query_service.list_member_requests(session, user=user, limit=limit, offset=offset)
    return [_to_member_view(item) for item in items]


@router.get(
    "/requests/{request_id}",
    response_model=MemberRequestView,
    summary="Get one of your prior authorization requests",
    responses={404: COMMON_ERROR_RESPONSES[404]},
)
def get_my_request(
    request_id: str,
    user: AuthenticatedUser = Depends(require_member),
    session: Session = Depends(get_db_session),
) -> MemberRequestView:
    req = query_service.get_member_request(session, user=user, request_id=request_id)
    return _to_member_view(req)
