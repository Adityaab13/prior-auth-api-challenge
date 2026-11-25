"""Unit tests for the pure functional submission domain core."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.submission import add_one_year, validate_submission
from app.domain.types import (
    CodeType,
    CoverageRecord,
    CoverageStatus,
    ProcedureInput,
    SubmissionCommand,
    SubmissionContext,
)


def _base_context(**overrides: object) -> SubmissionContext:
    defaults: dict[str, object] = {
        "practice_id": "prac-maine",
        "practice_timezone": "America/New_York",
        "member_exists": True,
        "is_patient_linked_to_practice": True,
        "coverages": (
            CoverageRecord(
                coverage_id="cov-1",
                member_id="mem-alice",
                plan_name="Gold PPO",
                effective_from=date(2025, 1, 1),
                effective_to=date(2028, 12, 31),
                status=CoverageStatus.ACTIVE,
            ),
        ),
        "now_utc": datetime(2026, 6, 15, 15, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return SubmissionContext(**defaults)  # type: ignore[arg-type]


def test_valid_submission_normalizes_codes_and_succeeds() -> None:
    cmd = SubmissionCommand(
        member_id="mem-alice",
        diagnosis_code=" m54.50 ",
        date_of_service=date(2026, 7, 1),
        procedures=(
            ProcedureInput(code_type=CodeType.CPT, code="72148", units=1),
            ProcedureInput(code_type=CodeType.HCPCS, code=" j1745 ", units=10),
        ),
        clinical_note="  Persistent lumbar radiculopathy.  ",
    )

    validated, issues = validate_submission(cmd, _base_context())

    assert issues == ()
    assert validated is not None
    assert validated.diagnosis_code == "M54.50"
    assert validated.procedures[1].code == "J1745"
    assert validated.clinical_note == "Persistent lumbar radiculopathy."


def test_timezone_hawaii_same_day_accepted_when_utc_is_next_day() -> None:
    # 2026-06-16 03:00 UTC is still 2026-06-15 17:00 in Pacific/Honolulu (UTC-10),
    # whereas in Maine (America/New_York, UTC-4) it is 2026-06-15 23:00.
    # At 2026-06-16 06:00 UTC: Hawaii is 2026-06-15 20:00, Maine is 2026-06-16 02:00.
    now_utc = datetime(2026, 6, 16, 6, 0, tzinfo=UTC)
    cmd = SubmissionCommand(
        member_id="mem-bob",
        diagnosis_code="M54.50",
        date_of_service=date(2026, 6, 15),
        procedures=(ProcedureInput(code_type=CodeType.CPT, code="72148", units=1),),
    )

    hawaii_ctx = _base_context(practice_id="prac-hawaii", practice_timezone="Pacific/Honolulu", now_utc=now_utc)
    validated_hi, issues_hi = validate_submission(cmd, hawaii_ctx)
    assert issues_hi == ()
    assert validated_hi is not None

    maine_ctx = _base_context(practice_id="prac-maine", practice_timezone="America/New_York", now_utc=now_utc)
    validated_me, issues_me = validate_submission(cmd, maine_ctx)
    assert validated_me is None
    assert [i.code for i in issues_me] == ["DATE_OF_SERVICE_IN_PAST"]


def test_date_more_than_one_year_out_rejected() -> None:
    cmd = SubmissionCommand(
        member_id="mem-alice",
        diagnosis_code="M54.50",
        date_of_service=date(2027, 6, 16),  # 1 year + 1 day after 2026-06-15
        procedures=(ProcedureInput(code_type=CodeType.CPT, code="72148", units=1),),
    )
    validated, issues = validate_submission(cmd, _base_context())
    assert validated is None
    assert [i.code for i in issues] == ["DATE_OF_SERVICE_TOO_FAR"]


def test_add_one_year_handles_leap_day() -> None:
    assert add_one_year(date(2024, 2, 29)) == date(2025, 2, 28)


def test_high_units_requires_explicit_confirmation() -> None:
    cmd_unconfirmed = SubmissionCommand(
        member_id="mem-alice",
        diagnosis_code="M54.50",
        date_of_service=date(2026, 7, 1),
        procedures=(ProcedureInput(code_type=CodeType.CPT, code="97110", units=45),),
    )
    validated, issues = validate_submission(cmd_unconfirmed, _base_context())
    assert validated is None
    assert [i.code for i in issues] == ["HIGH_UNITS_CONFIRMATION_REQUIRED"]
    assert issues[0].field == "procedures[0].units"

    cmd_confirmed = SubmissionCommand(
        member_id="mem-alice",
        diagnosis_code="M54.50",
        date_of_service=date(2026, 7, 1),
        procedures=(
            ProcedureInput(
                code_type=CodeType.CPT,
                code="97110",
                units=45,
                confirm_high_units=True,
            ),
        ),
    )
    validated_ok, issues_ok = validate_submission(cmd_confirmed, _base_context())
    assert issues_ok == ()
    assert validated_ok is not None
    assert validated_ok.procedures[0].high_units_confirmed is True


def test_accumulates_all_validation_errors_in_single_response() -> None:
    cmd = SubmissionCommand(
        member_id="mem-alice",
        diagnosis_code="INVALID",
        date_of_service=date(2025, 1, 1),  # in past + outside coverage if coverage starts 2026
        procedures=(
            ProcedureInput(code_type=CodeType.CPT, code="BAD", units=0),
            ProcedureInput(code_type=CodeType.HCPCS, code="99999", units=50),
        ),
    )
    ctx = _base_context(
        coverages=(
            CoverageRecord(
                coverage_id="cov-2",
                member_id="mem-alice",
                plan_name="Gold PPO",
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 12, 31),
                status=CoverageStatus.ACTIVE,
            ),
        )
    )

    validated, issues = validate_submission(cmd, ctx)
    assert validated is None
    codes = {i.code for i in issues}
    assert codes == {
        "DATE_OF_SERVICE_IN_PAST",
        "COVERAGE_INACTIVE",
        "INVALID_DIAGNOSIS_CODE",
        "INVALID_PROCEDURE_CODE",
        "INVALID_UNITS",
        "HIGH_UNITS_CONFIRMATION_REQUIRED",
    }


def test_unlinked_patient_does_not_leak_coverage_status() -> None:
    cmd = SubmissionCommand(
        member_id="mem-bob",
        diagnosis_code="M54.50",
        date_of_service=date(2026, 7, 1),
        procedures=(ProcedureInput(code_type=CodeType.CPT, code="72148", units=1),),
    )
    ctx = _base_context(is_patient_linked_to_practice=False, coverages=())

    validated, issues = validate_submission(cmd, ctx)
    assert validated is None
    assert [i.code for i in issues] == ["PATIENT_NOT_LINKED"]
