"""Imperative shell for queue claiming, admin reassignment, and atomic decisions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.domain.review import evaluate_decision, validate_claim, validate_reassignment
from app.domain.types import (
    AuditEventType,
    DomainError,
    PriorAuthRequest,
    ProcedureDecision,
    ProcedureDecisionInput,
    RequestStatus,
    UserRole,
)
from app.repos import audit_repo, reference_repo, request_repo, support_repo

logger = logging.getLogger(__name__)


def claim_next_request(
    session: Session,
    *,
    user: AuthenticatedUser,
    correlation_id: str,
    now: datetime | None = None,
) -> PriorAuthRequest:
    """Atomically claim the oldest eligible unassigned request for `user`."""
    ts = now or datetime.now(UTC)

    with session.begin():
        conflicted_members = reference_repo.get_conflicted_member_ids(session, user.user_id)
        row = request_repo.claim_next_unassigned_row(
            session,
            excluded_member_ids=conflicted_members,
        )
        if row is None:
            raise DomainError(
                code="QUEUE_EMPTY",
                message="No eligible unassigned requests are available in the queue.",
                status_code=404,
            )

        domain_req = request_repo.to_domain_request(row)
        err = validate_claim(
            request=domain_req,
            reviewer_id=user.user_id,
            is_treating_clinician=row.member_id in conflicted_members,
        )
        if err is not None:
            raise err

        row.status = RequestStatus.IN_REVIEW.value
        row.assigned_reviewer_id = user.user_id
        row.assigned_at = ts
        row.updated_at = ts
        session.flush()

        audit_repo.append_audit_event(
            session,
            request_id=row.id,
            event_type=AuditEventType.REQUEST_CLAIMED,
            actor_id=user.user_id,
            actor_role=user.role,
            occurred_at=ts,
            correlation_id=correlation_id,
            metadata={"claim_mode": "queue_next"},
        )
        claimed = request_repo.to_domain_request(row)

    logger.info(
        "Reviewer claimed next request from queue",
        extra={
            "event": "request_claimed",
            "actor_id": user.user_id,
            "actor_role": user.role.value,
            "prior_auth_id": claimed.id,
        },
    )
    return claimed


def claim_specific_request(
    session: Session,
    *,
    user: AuthenticatedUser,
    request_id: str,
    correlation_id: str,
    now: datetime | None = None,
) -> PriorAuthRequest:
    """Atomically claim a specific unassigned request by ID."""
    ts = now or datetime.now(UTC)

    with session.begin():
        row = request_repo.get_request_row(session, request_id, for_update=True)
        if row is None:
            raise DomainError(
                code="REQUEST_NOT_FOUND",
                message=f"Prior authorization request '{request_id}' was not found.",
                status_code=404,
            )

        domain_req = request_repo.to_domain_request(row)
        has_coi = reference_repo.is_treating_clinician(session, user.user_id, row.member_id)
        err = validate_claim(
            request=domain_req,
            reviewer_id=user.user_id,
            is_treating_clinician=has_coi,
        )
        if err is not None:
            raise err

        row.status = RequestStatus.IN_REVIEW.value
        row.assigned_reviewer_id = user.user_id
        row.assigned_at = ts
        row.updated_at = ts
        session.flush()

        audit_repo.append_audit_event(
            session,
            request_id=row.id,
            event_type=AuditEventType.REQUEST_CLAIMED,
            actor_id=user.user_id,
            actor_role=user.role,
            occurred_at=ts,
            correlation_id=correlation_id,
            metadata={"claim_mode": "direct"},
        )
        claimed = request_repo.to_domain_request(row)

    logger.info(
        "Reviewer claimed specific request",
        extra={
            "event": "request_claimed",
            "actor_id": user.user_id,
            "actor_role": user.role.value,
            "prior_auth_id": claimed.id,
        },
    )
    return claimed


def reassign_request(
    session: Session,
    *,
    admin_user: AuthenticatedUser,
    request_id: str,
    target_reviewer_id: str,
    reason: str | None,
    correlation_id: str,
    now: datetime | None = None,
) -> PriorAuthRequest:
    """Reassign an open request to another eligible reviewer (enforcing COI)."""
    ts = now or datetime.now(UTC)

    with session.begin():
        row = request_repo.get_request_row(session, request_id, for_update=True)
        if row is None:
            raise DomainError(
                code="REQUEST_NOT_FOUND",
                message=f"Prior authorization request '{request_id}' was not found.",
                status_code=404,
            )

        domain_req = request_repo.to_domain_request(row)
        target_is_reviewer = reference_repo.user_exists_with_role(
            session, target_reviewer_id, UserRole.REVIEWER
        )
        target_coi = (
            reference_repo.is_treating_clinician(session, target_reviewer_id, row.member_id)
            if target_is_reviewer
            else False
        )
        err = validate_reassignment(
            request=domain_req,
            target_reviewer_id=target_reviewer_id,
            target_exists_as_reviewer=target_is_reviewer,
            target_is_treating_clinician=target_coi,
        )
        if err is not None:
            raise err

        previous_reviewer_id = row.assigned_reviewer_id
        row.status = RequestStatus.IN_REVIEW.value
        row.assigned_reviewer_id = target_reviewer_id
        row.assigned_at = ts
        row.updated_at = ts
        session.flush()

        audit_repo.append_audit_event(
            session,
            request_id=row.id,
            event_type=AuditEventType.REQUEST_REASSIGNED,
            actor_id=admin_user.user_id,
            actor_role=admin_user.role,
            occurred_at=ts,
            correlation_id=correlation_id,
            metadata={
                "previous_reviewer_id": previous_reviewer_id,
                "new_reviewer_id": target_reviewer_id,
                "has_reason": bool(reason and reason.strip()),
            },
        )
        reassigned = request_repo.to_domain_request(row)

    logger.info(
        "Admin reassigned prior authorization request",
        extra={
            "event": "request_reassigned",
            "actor_id": admin_user.user_id,
            "actor_role": admin_user.role.value,
            "prior_auth_id": reassigned.id,
        },
    )
    return reassigned


def submit_decision(
    session: Session,
    *,
    user: AuthenticatedUser,
    request_id: str,
    decisions: tuple[ProcedureDecisionInput, ...],
    correlation_id: str,
    now: datetime | None = None,
) -> PriorAuthRequest:
    """Atomically persist per-procedure decisions, audit ledger entry, and office notification."""
    ts = now or datetime.now(UTC)

    with session.begin():
        row = request_repo.get_request_row(session, request_id, for_update=True)
        if row is None:
            raise DomainError(
                code="REQUEST_NOT_FOUND",
                message=f"Prior authorization request '{request_id}' was not found.",
                status_code=404,
            )

        domain_req = request_repo.to_domain_request(row)
        has_coi = reference_repo.is_treating_clinician(session, user.user_id, row.member_id)

        overall_status, resolved = evaluate_decision(
            request=domain_req,
            reviewer_id=user.user_id,
            is_treating_clinician=has_coi,
            decisions=decisions,
        )

        approved_count = 0
        denied_count = 0
        for proc_row in row.procedures:
            proc_decision, denial_reason = resolved[proc_row.id]
            proc_row.decision = proc_decision.value
            proc_row.denial_reason = denial_reason
            if proc_decision == ProcedureDecision.APPROVED:
                approved_count += 1
            else:
                denied_count += 1

        row.status = overall_status.value
        row.decided_by = user.user_id
        row.decided_at = ts
        row.updated_at = ts
        session.flush()

        audit_repo.append_audit_event(
            session,
            request_id=row.id,
            event_type=AuditEventType.REQUEST_DECIDED,
            actor_id=user.user_id,
            actor_role=user.role,
            occurred_at=ts,
            correlation_id=correlation_id,
            metadata={
                "overall_status": overall_status.value,
                "approved_count": approved_count,
                "denied_count": denied_count,
            },
        )

        # Wishlist: transactional outbox notification for the submitting provider office.
        support_repo.create_notification(
            session,
            request_id=row.id,
            practice_id=row.practice_id,
            event_type="decision_made",
            payload={
                "request_id": row.id,
                "status": overall_status.value,
                "approved_count": approved_count,
                "denied_count": denied_count,
                "decided_at": ts.isoformat(),
            },
            created_at=ts,
        )

        decided = request_repo.to_domain_request(row)

    logger.info(
        "Reviewer submitted final decision",
        extra={
            "event": "request_decided",
            "actor_id": user.user_id,
            "actor_role": user.role.value,
            "practice_id": decided.practice_id,
            "prior_auth_id": decided.id,
            "outcome": overall_status.value,
        },
    )
    return decided
