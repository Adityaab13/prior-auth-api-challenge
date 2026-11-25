"""Role-scoped read services enforcing strict data ownership per persona."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.domain.types import (
    AuditRecord,
    DomainError,
    NotificationRecord,
    PriorAuthRequest,
    RequestStatus,
    ReviewerWorkload,
    SubmissionAttemptRecord,
)
from app.repos import audit_repo, reference_repo, request_repo, support_repo


def list_provider_patients(
    session: Session,
    *,
    user: AuthenticatedUser,
) -> list[tuple[str, str]]:
    assert user.practice_id is not None
    members = reference_repo.list_patients_for_practice(session, user.practice_id)
    return [(m.id, m.full_name) for m in members]


def list_provider_requests(
    session: Session,
    *,
    user: AuthenticatedUser,
    status: RequestStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    assert user.practice_id is not None
    return request_repo.list_requests_for_practice(
        session, user.practice_id, status=status, limit=limit, offset=offset
    )


def get_provider_request(
    session: Session,
    *,
    user: AuthenticatedUser,
    request_id: str,
) -> PriorAuthRequest:
    row = request_repo.get_request_row(session, request_id)
    if row is None or row.practice_id != user.practice_id:
        raise DomainError(
            code="REQUEST_NOT_FOUND",
            message=f"Prior authorization request '{request_id}' was not found.",
            status_code=404,
        )
    return request_repo.to_domain_request(row)


def list_provider_notifications(
    session: Session,
    *,
    user: AuthenticatedUser,
    limit: int = 50,
) -> list[NotificationRecord]:
    assert user.practice_id is not None
    return support_repo.list_notifications_for_practice(session, user.practice_id, limit=limit)


def list_member_requests(
    session: Session,
    *,
    user: AuthenticatedUser,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    member_id = reference_repo.get_member_id_for_user(session, user.user_id)
    if member_id is None:
        return []
    return request_repo.list_requests_for_member(session, member_id, limit=limit, offset=offset)


def get_member_request(
    session: Session,
    *,
    user: AuthenticatedUser,
    request_id: str,
) -> PriorAuthRequest:
    member_id = reference_repo.get_member_id_for_user(session, user.user_id)
    row = request_repo.get_request_row(session, request_id)
    if member_id is None or row is None or row.member_id != member_id:
        raise DomainError(
            code="REQUEST_NOT_FOUND",
            message=f"Prior authorization request '{request_id}' was not found.",
            status_code=404,
        )
    return request_repo.to_domain_request(row)


def list_reviewer_queue(
    session: Session,
    *,
    user: AuthenticatedUser,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    """List unassigned requests eligible for `user`, automatically filtering out COI patients."""
    conflicted_members = reference_repo.get_conflicted_member_ids(session, user.user_id)
    return request_repo.list_unassigned_queue(
        session,
        excluded_member_ids=conflicted_members,
        limit=limit,
        offset=offset,
    )


def list_reviewer_requests(
    session: Session,
    *,
    user: AuthenticatedUser,
    status: RequestStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    return request_repo.list_requests_for_reviewer(
        session, user.user_id, status=status, limit=limit, offset=offset
    )


def get_reviewer_request(
    session: Session,
    *,
    user: AuthenticatedUser,
    request_id: str,
) -> PriorAuthRequest:
    row = request_repo.get_request_row(session, request_id)
    if row is None:
        raise DomainError(
            code="REQUEST_NOT_FOUND",
            message=f"Prior authorization request '{request_id}' was not found.",
            status_code=404,
        )
    if reference_repo.is_treating_clinician(session, user.user_id, row.member_id):
        raise DomainError(
            code="CONFLICT_OF_INTEREST",
            message="You cannot view or review a request for a patient you treat.",
            status_code=403,
        )
    if row.assigned_reviewer_id != user.user_id:
        raise DomainError(
            code="REQUEST_NOT_ASSIGNED_TO_REVIEWER",
            message="This request is not assigned to you.",
            status_code=403,
        )
    return request_repo.to_domain_request(row)


def list_admin_requests(
    session: Session,
    *,
    status: RequestStatus | None = None,
    practice_id: str | None = None,
    assigned_reviewer_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PriorAuthRequest]:
    return request_repo.list_all_requests(
        session,
        status=status,
        practice_id=practice_id,
        assigned_reviewer_id=assigned_reviewer_id,
        limit=limit,
        offset=offset,
    )


def get_admin_request_with_audit(
    session: Session,
    *,
    request_id: str,
) -> tuple[PriorAuthRequest, list[AuditRecord]]:
    row = request_repo.get_request_row(session, request_id)
    if row is None:
        raise DomainError(
            code="REQUEST_NOT_FOUND",
            message=f"Prior authorization request '{request_id}' was not found.",
            status_code=404,
        )
    events = audit_repo.list_audit_events_for_request(session, request_id)
    return request_repo.to_domain_request(row), events


def get_reviewer_workloads(session: Session) -> list[ReviewerWorkload]:
    return request_repo.get_reviewer_workloads(session)


def list_support_submission_attempts(
    session: Session,
    *,
    correlation_id: str | None = None,
    practice_id: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
) -> list[SubmissionAttemptRecord]:
    return support_repo.list_submission_attempts(
        session,
        correlation_id=correlation_id,
        practice_id=practice_id,
        outcome=outcome,
        limit=limit,
    )
