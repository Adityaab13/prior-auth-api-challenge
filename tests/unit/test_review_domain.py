"""Unit tests for the pure functional review, COI, reassignment, and decision rules."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.domain.review import evaluate_decision, validate_claim, validate_reassignment
from app.domain.types import (
    CodeType,
    DomainError,
    PriorAuthProcedure,
    PriorAuthRequest,
    ProcedureDecision,
    ProcedureDecisionInput,
    RequestStatus,
)


def _sample_request(
    *,
    status: RequestStatus = RequestStatus.IN_REVIEW,
    assigned_reviewer_id: str | None = "user-rev-nurse",
) -> PriorAuthRequest:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    return PriorAuthRequest(
        id="req-1",
        member_id="mem-alice",
        practice_id="prac-maine",
        submitted_by="user-prov-maine",
        diagnosis_code="M54.50",
        date_of_service=date(2026, 7, 1),
        clinical_note="Clinical note",
        status=status,
        assigned_reviewer_id=assigned_reviewer_id,
        assigned_at=now if assigned_reviewer_id else None,
        decided_by="user-rev-nurse" if status.is_final else None,
        decided_at=now if status.is_final else None,
        created_at=now,
        updated_at=now,
        procedures=(
            PriorAuthProcedure(
                id="proc-1",
                request_id="req-1",
                code_type=CodeType.CPT,
                code="72148",
                units=1,
                high_units_confirmed=False,
                decision=ProcedureDecision.PENDING,
                denial_reason=None,
            ),
            PriorAuthProcedure(
                id="proc-2",
                request_id="req-1",
                code_type=CodeType.CPT,
                code="97110",
                units=6,
                high_units_confirmed=False,
                decision=ProcedureDecision.PENDING,
                denial_reason=None,
            ),
        ),
    )


def test_claim_blocks_treating_physician_coi() -> None:
    req = _sample_request(status=RequestStatus.SUBMITTED, assigned_reviewer_id=None)
    err = validate_claim(
        request=req,
        reviewer_id="user-rev-dr-smith",
        is_treating_clinician=True,
    )
    assert err is not None
    assert err.code == "CONFLICT_OF_INTEREST"
    assert err.status_code == 403


def test_reassign_blocks_coi_and_final_requests() -> None:
    req = _sample_request(status=RequestStatus.IN_REVIEW, assigned_reviewer_id="user-rev-nurse")
    coi_err = validate_reassignment(
        request=req,
        target_reviewer_id="user-rev-dr-smith",
        target_exists_as_reviewer=True,
        target_is_treating_clinician=True,
    )
    assert coi_err is not None
    assert coi_err.code == "CONFLICT_OF_INTEREST"

    decided_req = _sample_request(status=RequestStatus.APPROVED, assigned_reviewer_id="user-rev-nurse")
    final_err = validate_reassignment(
        request=decided_req,
        target_reviewer_id="user-rev-other",
        target_exists_as_reviewer=True,
        target_is_treating_clinician=False,
    )
    assert final_err is not None
    assert final_err.code == "REQUEST_ALREADY_DECIDED"


def test_evaluate_decision_partial_approval() -> None:
    req = _sample_request()
    status, resolved = evaluate_decision(
        request=req,
        reviewer_id="user-rev-nurse",
        is_treating_clinician=False,
        decisions=(
            ProcedureDecisionInput(procedure_id="proc-1", decision=ProcedureDecision.APPROVED),
            ProcedureDecisionInput(
                procedure_id="proc-2",
                decision=ProcedureDecision.DENIED,
                denial_reason="Conservative therapy documentation missing.",
            ),
        ),
    )
    assert status == RequestStatus.PARTIALLY_APPROVED
    assert resolved["proc-1"] == (ProcedureDecision.APPROVED, None)
    assert resolved["proc-2"] == (
        ProcedureDecision.DENIED,
        "Conservative therapy documentation missing.",
    )


def test_evaluate_decision_requires_all_procedures_and_denial_reasons() -> None:
    req = _sample_request()
    with pytest.raises(DomainError) as exc_info:
        evaluate_decision(
            request=req,
            reviewer_id="user-rev-nurse",
            is_treating_clinician=False,
            decisions=(
                ProcedureDecisionInput(
                    procedure_id="proc-1",
                    decision=ProcedureDecision.DENIED,
                    denial_reason="   ",
                ),
            ),
        )
    codes = {issue.code for issue in exc_info.value.issues}
    assert codes == {"DENIAL_REASON_REQUIRED", "MISSING_PROCEDURE_DECISION"}


def test_evaluate_decision_rejects_already_decided_request() -> None:
    req = _sample_request(status=RequestStatus.APPROVED)
    with pytest.raises(DomainError) as exc_info:
        evaluate_decision(
            request=req,
            reviewer_id="user-rev-nurse",
            is_treating_clinician=False,
            decisions=(
                ProcedureDecisionInput(procedure_id="proc-1", decision=ProcedureDecision.APPROVED),
                ProcedureDecisionInput(procedure_id="proc-2", decision=ProcedureDecision.APPROVED),
            ),
        )
    assert exc_info.value.code == "REQUEST_ALREADY_DECIDED"
    assert exc_info.value.status_code == 409
