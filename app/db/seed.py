"""Idempotent synthetic seed data covering nationwide practices, coverage, and COI."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.db.models import (
    ClinicianPatientRow,
    MemberCoverageRow,
    MemberRow,
    PracticePatientRow,
    PracticeRow,
    UserRow,
)
from app.db.session import get_session_factory


def seed_database(session: Session) -> None:
    practices = [
        PracticeRow(
            id="prac-maine",
            name="Pine Tree Orthopedics (Maine)",
            timezone="America/New_York",
        ),
        PracticeRow(
            id="prac-hawaii",
            name="Aloha Pacific Medical Group (Hawaii)",
            timezone="Pacific/Honolulu",
        ),
    ]
    for practice in practices:
        session.merge(practice)
    session.flush()

    users = [
        UserRow(
            id="user-prov-maine",
            role="provider_office",
            display_name="Pine Tree Intake Desk",
            practice_id="prac-maine",
        ),
        UserRow(
            id="user-prov-hawaii",
            role="provider_office",
            display_name="Aloha Pacific Intake Desk",
            practice_id="prac-hawaii",
        ),
        UserRow(
            id="user-mem-alice",
            role="member",
            display_name="Alice Synthetic",
            practice_id=None,
        ),
        UserRow(
            id="user-mem-bob",
            role="member",
            display_name="Bob Synthetic",
            practice_id=None,
        ),
        UserRow(
            id="user-mem-carol",
            role="member",
            display_name="Carol Synthetic",
            practice_id=None,
        ),
        UserRow(
            id="user-rev-nurse",
            role="reviewer",
            display_name="Jordan Lee, RN",
            practice_id=None,
        ),
        UserRow(
            id="user-rev-dr-smith",
            role="reviewer",
            display_name="Dr. Morgan Smith, MD",
            practice_id=None,
        ),
        UserRow(
            id="user-admin-1",
            role="admin",
            display_name="Taylor Ops Admin",
            practice_id=None,
        ),
    ]
    for user in users:
        session.merge(user)
    session.flush()

    members = [
        MemberRow(
            id="mem-alice",
            user_id="user-mem-alice",
            full_name="Alice Synthetic",
            dob=date(1985, 4, 12),
        ),
        MemberRow(
            id="mem-bob",
            user_id="user-mem-bob",
            full_name="Bob Synthetic",
            dob=date(1979, 9, 23),
        ),
        MemberRow(
            id="mem-carol",
            user_id="user-mem-carol",
            full_name="Carol Synthetic",
            dob=date(1992, 1, 8),
        ),
    ]
    for member in members:
        session.merge(member)
    session.flush()

    coverages = [
        MemberCoverageRow(
            id="cov-alice-1",
            member_id="mem-alice",
            plan_name="Oxbridge Gold PPO",
            effective_from=date(2024, 1, 1),
            effective_to=date(2030, 12, 31),
            status="active",
        ),
        MemberCoverageRow(
            id="cov-bob-1",
            member_id="mem-bob",
            plan_name="Oxbridge Pacific HMO",
            effective_from=date(2024, 1, 1),
            effective_to=None,  # Open-ended active coverage
            status="active",
        ),
        MemberCoverageRow(
            id="cov-carol-expired",
            member_id="mem-carol",
            plan_name="Oxbridge Silver EPO",
            effective_from=date(2023, 1, 1),
            effective_to=date(2025, 12, 31),
            status="terminated",
        ),
    ]
    for coverage in coverages:
        session.merge(coverage)

    practice_patients = [
        PracticePatientRow(practice_id="prac-maine", member_id="mem-alice"),
        PracticePatientRow(practice_id="prac-maine", member_id="mem-carol"),
        PracticePatientRow(practice_id="prac-hawaii", member_id="mem-bob"),
    ]
    for link in practice_patients:
        session.merge(link)

    # Dr. Morgan Smith is both a reviewer and Alice's treating physician (COI scenario).
    clinician_patients = [
        ClinicianPatientRow(clinician_user_id="user-rev-dr-smith", member_id="mem-alice"),
    ]
    for link in clinician_patients:
        session.merge(link)

    session.commit()


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        seed_database(session)
    print("Synthetic seed data applied successfully.")


if __name__ == "__main__":
    main()
