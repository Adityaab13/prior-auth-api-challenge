"""Pure functional validation logic for prior authorization submissions.

Zero I/O, zero DB calls, zero system clock access. Every rule is evaluated in a
single pass so provider offices receive a complete, actionable list of fixes.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.domain.types import (
    CodeType,
    CoverageRecord,
    CoverageStatus,
    SubmissionCommand,
    SubmissionContext,
    ValidatedProcedure,
    ValidatedSubmission,
    ValidationIssue,
)

# ICD-10-CM: letter (excluding U) + 2 alphanumeric chars + optional dot and 1-4 chars.
_ICD10_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
# CPT: 5 numeric digits, or 4 digits + Category II/III alpha suffix.
_CPT_RE = re.compile(r"^[0-9]{4}[0-9A-Z]$")
# HCPCS Level II: single letter A-V followed by 4 numeric digits.
_HCPCS_RE = re.compile(r"^[A-V][0-9]{4}$")

HIGH_UNITS_THRESHOLD = 30
MAX_UNITS_HARD_LIMIT = 999
MAX_CLINICAL_NOTE_LENGTH = 10_000


def add_one_year(base_date: date) -> date:
    """Return the calendar date one year after base_date (Feb 29 clamps to Feb 28)."""
    try:
        return base_date.replace(year=base_date.year + 1)
    except ValueError:
        return date(base_date.year + 1, 2, 28)


def practice_local_today(now_utc: datetime, timezone_name: str) -> date:
    """Resolve 'today' in the submitting practice's local timezone (Maine to Hawaii)."""
    return now_utc.astimezone(ZoneInfo(timezone_name)).date()


def has_active_coverage_on(
    coverages: tuple[CoverageRecord, ...],
    service_date: date,
) -> bool:
    """Return True if at least one active coverage spans service_date."""
    return any(
        cov.status == CoverageStatus.ACTIVE
        and cov.effective_from <= service_date
        and (cov.effective_to is None or service_date <= cov.effective_to)
        for cov in coverages
    )


def normalize_code(raw: str) -> str:
    return raw.strip().upper()


def is_valid_icd10(code: str) -> bool:
    return bool(_ICD10_RE.match(code))


def is_valid_procedure_code(code_type: CodeType, code: str) -> bool:
    if code_type == CodeType.CPT:
        return bool(_CPT_RE.match(code))
    if code_type == CodeType.HCPCS:
        return bool(_HCPCS_RE.match(code))
    return False


def validate_submission(
    cmd: SubmissionCommand,
    ctx: SubmissionContext,
) -> tuple[ValidatedSubmission | None, tuple[ValidationIssue, ...]]:
    """Validate a submission command against all business rules.

    Returns `(validated_submission, ())` on success, or `(None, issues)` with
    every detected problem so the provider office can fix everything in one step.
    """
    issues: list[ValidationIssue] = []

    # 1. Patient-practice relationship (checked first; avoid leaking unlinked member coverage).
    patient_linked = ctx.member_exists and ctx.is_patient_linked_to_practice
    if not patient_linked:
        issues.append(
            ValidationIssue(
                field="member_id",
                code="PATIENT_NOT_LINKED",
                message=(
                    f"Member '{cmd.member_id}' is not linked to your practice. "
                    "Confirm the member ID or contact Provider Relations to link the patient."
                ),
            )
        )

    # 2. Date of service evaluated in the practice's local timezone (nationwide clinics).
    local_today = practice_local_today(ctx.now_utc, ctx.practice_timezone)
    max_service_date = add_one_year(local_today)

    if cmd.date_of_service < local_today:
        issues.append(
            ValidationIssue(
                field="date_of_service",
                code="DATE_OF_SERVICE_IN_PAST",
                message=(
                    f"Date of service ({cmd.date_of_service.isoformat()}) is in the past "
                    f"for practice timezone {ctx.practice_timezone} (local date: "
                    f"{local_today.isoformat()}). Retroactive reviews are not accepted here."
                ),
            )
        )
    elif cmd.date_of_service > max_service_date:
        issues.append(
            ValidationIssue(
                field="date_of_service",
                code="DATE_OF_SERVICE_TOO_FAR",
                message=(
                    f"Date of service ({cmd.date_of_service.isoformat()}) is more than one year "
                    f"out. Must be on or before {max_service_date.isoformat()}."
                ),
            )
        )

    # 3. Active coverage on date of service (only exposed for patients linked to this practice).
    if patient_linked and not has_active_coverage_on(ctx.coverages, cmd.date_of_service):
        issues.append(
            ValidationIssue(
                field="date_of_service",
                code="COVERAGE_INACTIVE",
                message=(
                    f"Patient does not have active coverage on "
                    f"{cmd.date_of_service.isoformat()}. Verify eligibility dates before resubmitting."
                ),
            )
        )

    # 4. Diagnosis code (ICD-10-CM).
    diagnosis_code = normalize_code(cmd.diagnosis_code)
    if not is_valid_icd10(diagnosis_code):
        issues.append(
            ValidationIssue(
                field="diagnosis_code",
                code="INVALID_DIAGNOSIS_CODE",
                message=(
                    f"Diagnosis code '{cmd.diagnosis_code}' is not a valid ICD-10-CM code "
                    "(examples: 'M54.50', 'E11.9', 'S83.511A')."
                ),
            )
        )

    # 5. Procedures (CPT/HCPCS format, units bounds, typo threshold, duplicates).
    validated_procedures: list[ValidatedProcedure] = []
    if not cmd.procedures:
        issues.append(
            ValidationIssue(
                field="procedures",
                code="PROCEDURES_REQUIRED",
                message="At least one procedure (CPT or HCPCS) is required.",
            )
        )
    else:
        seen_codes: set[tuple[CodeType, str]] = set()
        for idx, proc in enumerate(cmd.procedures):
            prefix = f"procedures[{idx}]"
            code = normalize_code(proc.code)

            if not is_valid_procedure_code(proc.code_type, code):
                example = "72148" if proc.code_type == CodeType.CPT else "J1745"
                issues.append(
                    ValidationIssue(
                        field=f"{prefix}.code",
                        code="INVALID_PROCEDURE_CODE",
                        message=(
                            f"Code '{proc.code}' is not a valid {proc.code_type.value} code "
                            f"(example: '{example}')."
                        ),
                    )
                )

            dedup_key = (proc.code_type, code)
            if dedup_key in seen_codes:
                issues.append(
                    ValidationIssue(
                        field=f"{prefix}.code",
                        code="DUPLICATE_PROCEDURE_CODE",
                        message=(
                            f"Duplicate {proc.code_type.value} code '{code}' in request. "
                            "Combine units into a single procedure line."
                        ),
                    )
                )
            else:
                seen_codes.add(dedup_key)

            if proc.units <= 0:
                issues.append(
                    ValidationIssue(
                        field=f"{prefix}.units",
                        code="INVALID_UNITS",
                        message="Units must be a positive integer greater than 0.",
                    )
                )
            elif proc.units > MAX_UNITS_HARD_LIMIT:
                issues.append(
                    ValidationIssue(
                        field=f"{prefix}.units",
                        code="UNITS_EXCEED_MAX",
                        message=(
                            f"Units ({proc.units}) exceed the maximum allowed limit of "
                            f"{MAX_UNITS_HARD_LIMIT}."
                        ),
                    )
                )
            elif proc.units > HIGH_UNITS_THRESHOLD and not proc.confirm_high_units:
                issues.append(
                    ValidationIssue(
                        field=f"{prefix}.units",
                        code="HIGH_UNITS_CONFIRMATION_REQUIRED",
                        message=(
                            f"Units ({proc.units}) exceed {HIGH_UNITS_THRESHOLD}, which is usually "
                            f"a typo. Verify the quantity or resubmit with "
                            f"{prefix}.confirm_high_units=true if intentional."
                        ),
                    )
                )

            validated_procedures.append(
                ValidatedProcedure(
                    code_type=proc.code_type,
                    code=code,
                    units=proc.units,
                    high_units_confirmed=proc.units > HIGH_UNITS_THRESHOLD
                    and proc.confirm_high_units,
                )
            )

    # 6. Clinical note (optional).
    cleaned_note: str | None = None
    if cmd.clinical_note is not None:
        stripped = cmd.clinical_note.strip()
        if len(stripped) > MAX_CLINICAL_NOTE_LENGTH:
            issues.append(
                ValidationIssue(
                    field="clinical_note",
                    code="CLINICAL_NOTE_TOO_LONG",
                    message=(
                        f"Clinical note exceeds the {MAX_CLINICAL_NOTE_LENGTH}-character limit."
                    ),
                )
            )
        elif stripped:
            cleaned_note = stripped

    if issues:
        return None, tuple(issues)

    return (
        ValidatedSubmission(
            member_id=cmd.member_id,
            practice_id=ctx.practice_id,
            diagnosis_code=diagnosis_code,
            date_of_service=cmd.date_of_service,
            procedures=tuple(validated_procedures),
            clinical_note=cleaned_note,
        ),
        (),
    )
