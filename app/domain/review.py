"""Pure functional review, conflict-of-interest, reassignment, and decision rules."""

from __future__ import annotations

from app.domain.types import (
    DomainError,
    PriorAuthRequest,
    ProcedureDecision,
    ProcedureDecisionInput,
    RequestStatus,
    ValidationIssue,
)


def validate_claim(
    *,
    request: PriorAuthRequest,
    reviewer_id: str,
    is_treating_clinician: bool,
) -> DomainError | None:
    """Validate whether `reviewer_id` may claim `request` from the queue."""
    if request.status.is_final:
        return DomainError(
            code="REQUEST_ALREADY_DECIDED",
            message="This prior authorization request has already been decided and is final.",
            status_code=409,
        )

    if is_treating_clinician:
        return DomainError(
            code="CONFLICT_OF_INTEREST",
            message="You cannot review a request for a patient you treat.",
            status_code=403,
        )

    if request.status == RequestStatus.IN_REVIEW:
        if request.assigned_reviewer_id == reviewer_id:
            return DomainError(
                code="REQUEST_ALREADY_ASSIGNED_TO_YOU",
                message="You have already claimed this prior authorization request.",
                status_code=409,
            )
        return DomainError(
            code="REQUEST_ALREADY_CLAIMED",
            message="This request is currently assigned to another reviewer.",
            status_code=409,
        )

    return None


def validate_reassignment(
    *,
    request: PriorAuthRequest,
    target_reviewer_id: str,
    target_exists_as_reviewer: bool,
    target_is_treating_clinician: bool,
) -> DomainError | None:
    """Validate an admin reassigning a request to another reviewer."""
    if request.status.is_final:
        return DomainError(
            code="REQUEST_ALREADY_DECIDED",
            message="Cannot reassign a request that has already been decided.",
            status_code=409,
        )

    if not target_exists_as_reviewer:
        return DomainError(
            code="INVALID_REVIEWER",
            message=f"User '{target_reviewer_id}' is not a registered reviewer.",
            status_code=422,
            issues=(
                ValidationIssue(
                    field="reviewer_id",
                    code="INVALID_REVIEWER",
                    message=f"User '{target_reviewer_id}' is not a registered reviewer.",
                ),
            ),
        )

    if target_is_treating_clinician:
        return DomainError(
            code="CONFLICT_OF_INTEREST",
            message=(
                f"Reviewer '{target_reviewer_id}' treats this patient and cannot be "
                "assigned to this request."
            ),
            status_code=409,
        )

    if request.assigned_reviewer_id == target_reviewer_id:
        return DomainError(
            code="ALREADY_ASSIGNED_TO_REVIEWER",
            message=f"Request is already assigned to reviewer '{target_reviewer_id}'.",
            status_code=409,
        )

    return None


def derive_request_status(decisions: tuple[ProcedureDecision, ...]) -> RequestStatus:
    """Derive the overall request status from per-procedure outcomes."""
    if all(d == ProcedureDecision.APPROVED for d in decisions):
        return RequestStatus.APPROVED
    if all(d == ProcedureDecision.DENIED for d in decisions):
        return RequestStatus.DENIED
    return RequestStatus.PARTIALLY_APPROVED


def evaluate_decision(
    *,
    request: PriorAuthRequest,
    reviewer_id: str,
    is_treating_clinician: bool,
    decisions: tuple[ProcedureDecisionInput, ...],
) -> tuple[RequestStatus, dict[str, tuple[ProcedureDecision, str | None]]]:
    """Validate and compute an atomic decision for every procedure in `request`.

    Raises `DomainError` if any precondition or procedure-level validation fails.
    """
    if request.status.is_final:
        raise DomainError(
            code="REQUEST_ALREADY_DECIDED",
            message="Decisions are final and cannot be changed.",
            status_code=409,
        )

    if is_treating_clinician:
        raise DomainError(
            code="CONFLICT_OF_INTEREST",
            message="You cannot decide a request for a patient you treat.",
            status_code=403,
        )

    if request.status != RequestStatus.IN_REVIEW or request.assigned_reviewer_id != reviewer_id:
        raise DomainError(
            code="REQUEST_NOT_ASSIGNED_TO_REVIEWER",
            message="You must claim this request before submitting a decision.",
            status_code=403,
        )

    expected_ids = {proc.id for proc in request.procedures}
    seen_ids: set[str] = set()
    issues: list[ValidationIssue] = []
    resolved: dict[str, tuple[ProcedureDecision, str | None]] = {}

    for idx, item in enumerate(decisions):
        prefix = f"decisions[{idx}]"

        if item.procedure_id not in expected_ids:
            issues.append(
                ValidationIssue(
                    field=f"{prefix}.procedure_id",
                    code="UNKNOWN_PROCEDURE_ID",
                    message=f"Procedure '{item.procedure_id}' does not belong to this request.",
                )
            )
            continue

        if item.procedure_id in seen_ids:
            issues.append(
                ValidationIssue(
                    field=f"{prefix}.procedure_id",
                    code="DUPLICATE_PROCEDURE_DECISION",
                    message=f"Duplicate decision provided for procedure '{item.procedure_id}'.",
                )
            )
            continue
        seen_ids.add(item.procedure_id)

        if item.decision not in {ProcedureDecision.APPROVED, ProcedureDecision.DENIED}:
            issues.append(
                ValidationIssue(
                    field=f"{prefix}.decision",
                    code="INVALID_PROCEDURE_DECISION",
                    message="Each procedure decision must be 'approved' or 'denied'.",
                )
            )
            continue

        cleaned_reason = item.denial_reason.strip() if item.denial_reason else None

        if item.decision == ProcedureDecision.DENIED and not cleaned_reason:
            issues.append(
                ValidationIssue(
                    field=f"{prefix}.denial_reason",
                    code="DENIAL_REASON_REQUIRED",
                    message=(
                        f"A non-empty denial_reason is required when denying procedure "
                        f"'{item.procedure_id}'."
                    ),
                )
            )
            continue

        if item.decision == ProcedureDecision.APPROVED and cleaned_reason:
            issues.append(
                ValidationIssue(
                    field=f"{prefix}.denial_reason",
                    code="UNEXPECTED_DENIAL_REASON",
                    message=(
                        f"denial_reason must not be set when approving procedure "
                        f"'{item.procedure_id}'."
                    ),
                )
            )
            continue

        resolved[item.procedure_id] = (item.decision, cleaned_reason)

    missing_ids = sorted(expected_ids - seen_ids)
    for missing_id in missing_ids:
        issues.append(
            ValidationIssue(
                field="decisions",
                code="MISSING_PROCEDURE_DECISION",
                message=(
                    f"A decision is required for procedure '{missing_id}'. "
                    "All procedures must be decided together."
                ),
            )
        )

    if issues:
        raise DomainError(
            code="INVALID_DECISION_PAYLOAD",
            message="One or more procedure decisions are invalid.",
            status_code=422,
            issues=tuple(issues),
        )

    overall_status = derive_request_status(tuple(d for d, _ in resolved.values()))
    return overall_status, resolved
