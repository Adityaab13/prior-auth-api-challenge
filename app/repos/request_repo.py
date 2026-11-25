"""SQLAlchemy repository functions for prior authorization requests and procedures."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import PriorAuthProcedureRow, PriorAuthRequestRow, UserRow
from app.domain.types import (
    CodeType,
    PriorAuthProcedure,
    PriorAuthRequest,
    ProcedureDecision,
    RequestStatus,
    ReviewerWorkload,
    UserRole,
    ValidatedSubmission,
)


def to_domain_request(row: PriorAuthRequestRow) -> PriorAuthRequest:
    """Explicit boundary mapper: SQLAlchemy ORM row -> immutable domain dataclass."""
    return PriorAuthRequest(
        id=row.id,
        member_id=row.member_id,
        practice_id=row.practice_id,
        submitted_by=row.submitted_by,
        diagnosis_code=row.diagnosis_code,
        date_of_service=row.date_of_service,
        clinical_note=row.clinical_note,
        status=RequestStatus(row.status),
        assigned_reviewer_id=row.assigned_reviewer_id,
        assigned_at=row.assigned_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        procedures=tuple(
            PriorAuthProcedure(
                id=proc.id,
                request_id=proc.request_id,
                code_type=CodeType(proc.code_type),
                code=proc.code,
                units=proc.units,
                high_units_confirmed=proc.high_units_confirmed,
                decision=ProcedureDecision(proc.decision),
                denial_reason=proc.denial_reason,
            )
            for proc in row.procedures
        ),
    )


def get_by_idempotency_key(
    session: Session,
    *,
    practice_id: str,
    idempotency_key: str,
) -> PriorAuthRequest | None:
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(
            PriorAuthRequestRow.practice_id == practice_id,
            PriorAuthRequestRow.idempotency_key == idempotency_key,
        )
    )
    row = session.scalar(stmt)
    return to_domain_request(row) if row is not None else None


def create_request(
    session: Session,
    *,
    validated: ValidatedSubmission,
    submitted_by: str,
    idempotency_key: str | None,
    now: datetime,
) -> PriorAuthRequest:
    request_id = str(uuid.uuid4())
    req_row = PriorAuthRequestRow(
        id=request_id,
        member_id=validated.member_id,
        practice_id=validated.practice_id,
        submitted_by=submitted_by,
        diagnosis_code=validated.diagnosis_code,
        date_of_service=validated.date_of_service,
        clinical_note=validated.clinical_note,
        status=RequestStatus.SUBMITTED.value,
        assigned_reviewer_id=None,
        assigned_at=None,
        decided_by=None,
        decided_at=None,
        idempotency_key=idempotency_key,
        created_at=now,
        updated_at=now,
        procedures=[
            PriorAuthProcedureRow(
                id=str(uuid.uuid4()),
                request_id=request_id,
                code_type=proc.code_type.value,
                code=proc.code,
                units=proc.units,
                high_units_confirmed=proc.high_units_confirmed,
                decision=ProcedureDecision.PENDING.value,
                denial_reason=None,
            )
            for proc in validated.procedures
        ],
    )
    session.add(req_row)
    session.flush()
    return to_domain_request(req_row)


def get_request_row(
    session: Session,
    request_id: str,
    *,
    for_update: bool = False,
) -> PriorAuthRequestRow | None:
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(PriorAuthRequestRow.id == request_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def claim_next_unassigned_row(
    session: Session,
    *,
    excluded_member_ids: frozenset[str],
) -> PriorAuthRequestRow | None:
    """Lock and return the oldest unassigned request without COI using SKIP LOCKED."""
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(PriorAuthRequestRow.status == RequestStatus.SUBMITTED.value)
        .order_by(PriorAuthRequestRow.created_at.asc(), PriorAuthRequestRow.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if excluded_member_ids:
        stmt = stmt.where(PriorAuthRequestRow.member_id.notin_(excluded_member_ids))
    return session.scalar(stmt)


def list_unassigned_queue(
    session: Session,
    *,
    excluded_member_ids: frozenset[str],
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    """List unassigned requests in FIFO order, excluding patients the reviewer treats."""
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(PriorAuthRequestRow.status == RequestStatus.SUBMITTED.value)
        .order_by(PriorAuthRequestRow.created_at.asc(), PriorAuthRequestRow.id.asc())
        .limit(limit)
        .offset(offset)
    )
    if excluded_member_ids:
        stmt = stmt.where(PriorAuthRequestRow.member_id.notin_(excluded_member_ids))
    return [to_domain_request(row) for row in session.scalars(stmt).all()]


def list_requests_for_practice(
    session: Session,
    practice_id: str,
    *,
    status: RequestStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(PriorAuthRequestRow.practice_id == practice_id)
        .order_by(PriorAuthRequestRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(PriorAuthRequestRow.status == status.value)
    return [to_domain_request(row) for row in session.scalars(stmt).all()]


def list_requests_for_member(
    session: Session,
    member_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(PriorAuthRequestRow.member_id == member_id)
        .order_by(PriorAuthRequestRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [to_domain_request(row) for row in session.scalars(stmt).all()]


def list_requests_for_reviewer(
    session: Session,
    reviewer_id: str,
    *,
    status: RequestStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .where(PriorAuthRequestRow.assigned_reviewer_id == reviewer_id)
        .order_by(PriorAuthRequestRow.assigned_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(PriorAuthRequestRow.status == status.value)
    return [to_domain_request(row) for row in session.scalars(stmt).all()]


def list_all_requests(
    session: Session,
    *,
    status: RequestStatus | None = None,
    practice_id: str | None = None,
    assigned_reviewer_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    stmt = (
        select(PriorAuthRequestRow)
        .options(selectinload(PriorAuthRequestRow.procedures))
        .order_by(PriorAuthRequestRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(PriorAuthRequestRow.status == status.value)
    if practice_id is not None:
        stmt = stmt.where(PriorAuthRequestRow.practice_id == practice_id)
    if assigned_reviewer_id is not None:
        stmt = stmt.where(PriorAuthRequestRow.assigned_reviewer_id == assigned_reviewer_id)
    return [to_domain_request(row) for row in session.scalars(stmt).all()]


def get_reviewer_workloads(session: Session) -> list[ReviewerWorkload]:
    """Return open (in_review) request counts for every registered reviewer."""
    stmt = (
        select(
            UserRow.id,
            UserRow.display_name,
            func.count(PriorAuthRequestRow.id).label("open_count"),
        )
        .outerjoin(
            PriorAuthRequestRow,
            (PriorAuthRequestRow.assigned_reviewer_id == UserRow.id)
            & (PriorAuthRequestRow.status == RequestStatus.IN_REVIEW.value),
        )
        .where(UserRow.role == UserRole.REVIEWER.value)
        .group_by(UserRow.id, UserRow.display_name)
        .order_by(UserRow.id.asc())
    )
    return [
        ReviewerWorkload(
            reviewer_id=row.id,
            display_name=row.display_name,
            open_request_count=int(row.open_count),
        )
        for row in session.execute(stmt).all()
    ]
