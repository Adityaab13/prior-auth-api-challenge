"""Imperative shell for prior authorization submissions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.domain.submission import validate_submission
from app.domain.types import (
    AuditEventType,
    DomainError,
    PriorAuthRequest,
    SubmissionCommand,
    SubmissionContext,
)
from app.repos import audit_repo, reference_repo, request_repo, support_repo

logger = logging.getLogger(__name__)


def submit_prior_auth(
    session: Session,
    *,
    user: AuthenticatedUser,
    cmd: SubmissionCommand,
    correlation_id: str,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[PriorAuthRequest, bool]:
    """Validate and submit a prior authorization request.

    Returns `(request, is_idempotent_replay)`.
    Rejected submissions are persisted in the PHI-free `submission_attempts` table
    so the support desk can diagnose why a clinic's submission failed.
    """
    if not user.practice_id:
        raise DomainError(
            code="MISSING_PRACTICE_ID",
            message="Provider office caller must be associated with a practice.",
            status_code=403,
        )

    ts = now or datetime.now(UTC)
    cleaned_idem_key = idempotency_key.strip() if idempotency_key else None

    with session.begin():
        if cleaned_idem_key:
            existing = request_repo.get_by_idempotency_key(
                session,
                practice_id=user.practice_id,
                idempotency_key=cleaned_idem_key,
            )
            if existing is not None:
                logger.info(
                    "Idempotent replay returned existing prior authorization",
                    extra={
                        "event": "submission_idempotent_replay",
                        "actor_id": user.user_id,
                        "actor_role": user.role.value,
                        "practice_id": user.practice_id,
                        "prior_auth_id": existing.id,
                    },
                )
                return existing, True

        practice_tz = reference_repo.get_practice_timezone(session, user.practice_id)
        if practice_tz is None:
            raise DomainError(
                code="UNKNOWN_PRACTICE",
                message=f"Practice '{user.practice_id}' is not registered.",
                status_code=403,
            )

        exists = reference_repo.member_exists(session, cmd.member_id)
        linked = (
            reference_repo.is_patient_linked_to_practice(session, user.practice_id, cmd.member_id)
            if exists
            else False
        )
        coverages = (
            reference_repo.get_member_coverages(session, cmd.member_id) if linked else ()
        )

        ctx = SubmissionContext(
            practice_id=user.practice_id,
            practice_timezone=practice_tz,
            member_exists=exists,
            is_patient_linked_to_practice=linked,
            coverages=coverages,
            now_utc=ts,
        )

        validated, issues = validate_submission(cmd, ctx)

        if issues or validated is None:
            error_codes = tuple(sorted({issue.code for issue in issues}))
            support_repo.record_submission_attempt(
                session,
                correlation_id=correlation_id,
                practice_id=user.practice_id,
                actor_id=user.user_id,
                occurred_at=ts,
                outcome="rejected",
                error_codes=error_codes,
                prior_auth_id=None,
            )
            # Commit the support attempt record before raising validation error.
            logger.info(
                "Prior authorization submission rejected",
                extra={
                    "event": "submission_rejected",
                    "actor_id": user.user_id,
                    "actor_role": user.role.value,
                    "practice_id": user.practice_id,
                    "outcome": "rejected",
                    "error_codes": list(error_codes),
                },
            )
            rejection_error = DomainError(
                code="VALIDATION_FAILED",
                message="Submission could not be accepted. Fix the listed issues and resubmit.",
                status_code=422,
                issues=issues,
            )
        else:
            created = request_repo.create_request(
                session,
                validated=validated,
                submitted_by=user.user_id,
                idempotency_key=cleaned_idem_key,
                now=ts,
            )
            audit_repo.append_audit_event(
                session,
                request_id=created.id,
                event_type=AuditEventType.REQUEST_SUBMITTED,
                actor_id=user.user_id,
                actor_role=user.role,
                occurred_at=ts,
                correlation_id=correlation_id,
                metadata={
                    "practice_id": user.practice_id,
                    "procedure_count": len(created.procedures),
                    "high_units_confirmed_count": sum(
                        1 for p in created.procedures if p.high_units_confirmed
                    ),
                },
            )
            support_repo.record_submission_attempt(
                session,
                correlation_id=correlation_id,
                practice_id=user.practice_id,
                actor_id=user.user_id,
                occurred_at=ts,
                outcome="accepted",
                error_codes=(),
                prior_auth_id=created.id,
            )
            logger.info(
                "Prior authorization submission accepted",
                extra={
                    "event": "submission_accepted",
                    "actor_id": user.user_id,
                    "actor_role": user.role.value,
                    "practice_id": user.practice_id,
                    "prior_auth_id": created.id,
                    "outcome": "accepted",
                },
            )
            return created, False

    raise rejection_error
