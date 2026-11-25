"""Read-only SQLAlchemy repository functions for seeded reference entities."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.models import (
    ClinicianPatientRow,
    MemberCoverageRow,
    MemberRow,
    PracticePatientRow,
    PracticeRow,
    UserRow,
)
from app.domain.types import CoverageRecord, CoverageStatus, UserRole


def get_practice_timezone(session: Session, practice_id: str) -> str | None:
    stmt = select(PracticeRow.timezone).where(PracticeRow.id == practice_id)
    return session.scalar(stmt)


def member_exists(session: Session, member_id: str) -> bool:
    stmt = select(exists().where(MemberRow.id == member_id))
    return bool(session.scalar(stmt))


def get_member_id_for_user(session: Session, user_id: str) -> str | None:
    stmt = select(MemberRow.id).where(MemberRow.user_id == user_id)
    return session.scalar(stmt)


def is_patient_linked_to_practice(session: Session, practice_id: str, member_id: str) -> bool:
    stmt = select(
        exists().where(
            PracticePatientRow.practice_id == practice_id,
            PracticePatientRow.member_id == member_id,
        )
    )
    return bool(session.scalar(stmt))


def get_member_coverages(session: Session, member_id: str) -> tuple[CoverageRecord, ...]:
    stmt = (
        select(MemberCoverageRow)
        .where(MemberCoverageRow.member_id == member_id)
        .order_by(MemberCoverageRow.effective_from.asc())
    )
    rows = session.scalars(stmt).all()
    return tuple(
        CoverageRecord(
            coverage_id=r.id,
            member_id=r.member_id,
            plan_name=r.plan_name,
            effective_from=r.effective_from,
            effective_to=r.effective_to,
            status=CoverageStatus(r.status),
        )
        for r in rows
    )


def is_treating_clinician(session: Session, clinician_user_id: str, member_id: str) -> bool:
    stmt = select(
        exists().where(
            ClinicianPatientRow.clinician_user_id == clinician_user_id,
            ClinicianPatientRow.member_id == member_id,
        )
    )
    return bool(session.scalar(stmt))


def get_conflicted_member_ids(session: Session, clinician_user_id: str) -> frozenset[str]:
    stmt = select(ClinicianPatientRow.member_id).where(
        ClinicianPatientRow.clinician_user_id == clinician_user_id
    )
    return frozenset(session.scalars(stmt).all())


def user_exists_with_role(session: Session, user_id: str, role: UserRole) -> bool:
    stmt = select(exists().where(UserRow.id == user_id, UserRow.role == role.value))
    return bool(session.scalar(stmt))


def get_user_by_id(session: Session, user_id: str) -> UserRow | None:
    return session.get(UserRow, user_id)


def list_all_users(session: Session) -> list[UserRow]:
    stmt = select(UserRow).order_by(UserRow.role.asc(), UserRow.id.asc())
    return list(session.scalars(stmt).all())


def list_patients_for_practice(session: Session, practice_id: str) -> list[MemberRow]:
    """Return members linked to `practice_id` (read-only lookup for provider portal)."""
    stmt = (
        select(MemberRow)
        .join(PracticePatientRow, PracticePatientRow.member_id == MemberRow.id)
        .where(PracticePatientRow.practice_id == practice_id)
        .order_by(MemberRow.full_name.asc())
    )
    return list(session.scalars(stmt).all())
