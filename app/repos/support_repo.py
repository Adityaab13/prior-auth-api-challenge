"""Repositories for PHI-free support submission attempts and decision notifications."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import NotificationRow, SubmissionAttemptRow
from app.domain.types import NotificationRecord, SubmissionAttemptRecord


def record_submission_attempt(
    session: Session,
    *,
    correlation_id: str,
    practice_id: str,
    actor_id: str,
    occurred_at: datetime,
    outcome: str,
    error_codes: tuple[str, ...],
    prior_auth_id: str | None = None,
) -> SubmissionAttemptRecord:
    row = SubmissionAttemptRow(
        id=str(uuid.uuid4()),
        correlation_id=correlation_id,
        practice_id=practice_id,
        actor_id=actor_id,
        occurred_at=occurred_at,
        outcome=outcome,
        error_codes=list(error_codes),
        prior_auth_id=prior_auth_id,
    )
    session.add(row)
    session.flush()
    return _to_attempt_record(row)


def list_submission_attempts(
    session: Session,
    *,
    correlation_id: str | None = None,
    practice_id: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
) -> list[SubmissionAttemptRecord]:
    stmt = (
        select(SubmissionAttemptRow)
        .order_by(SubmissionAttemptRow.occurred_at.desc())
        .limit(limit)
    )
    if correlation_id is not None:
        stmt = stmt.where(SubmissionAttemptRow.correlation_id == correlation_id)
    if practice_id is not None:
        stmt = stmt.where(SubmissionAttemptRow.practice_id == practice_id)
    if outcome is not None:
        stmt = stmt.where(SubmissionAttemptRow.outcome == outcome)
    return [_to_attempt_record(row) for row in session.scalars(stmt).all()]


def create_notification(
    session: Session,
    *,
    request_id: str,
    practice_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_at: datetime,
) -> NotificationRecord:
    row = NotificationRow(
        id=str(uuid.uuid4()),
        request_id=request_id,
        practice_id=practice_id,
        event_type=event_type,
        payload_json=payload,
        status="pending",
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    return _to_notification_record(row)


def list_notifications_for_practice(
    session: Session,
    practice_id: str,
    *,
    limit: int = 50,
) -> list[NotificationRecord]:
    stmt = (
        select(NotificationRow)
        .where(NotificationRow.practice_id == practice_id)
        .order_by(NotificationRow.created_at.desc())
        .limit(limit)
    )
    return [_to_notification_record(row) for row in session.scalars(stmt).all()]


def _to_attempt_record(row: SubmissionAttemptRow) -> SubmissionAttemptRecord:
    return SubmissionAttemptRecord(
        id=row.id,
        correlation_id=row.correlation_id,
        practice_id=row.practice_id,
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        outcome=row.outcome,
        error_codes=tuple(row.error_codes or ()),
        prior_auth_id=row.prior_auth_id,
    )


def _to_notification_record(row: NotificationRow) -> NotificationRecord:
    return NotificationRecord(
        id=row.id,
        request_id=row.request_id,
        practice_id=row.practice_id,
        event_type=row.event_type,
        payload=dict(row.payload_json or {}),
        status=row.status,
        created_at=row.created_at,
    )
