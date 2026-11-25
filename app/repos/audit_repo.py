"""Append-only compliance audit ledger repository."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEventRow
from app.domain.types import AuditEventType, AuditRecord, UserRole


def append_audit_event(
    session: Session,
    *,
    request_id: str,
    event_type: AuditEventType,
    actor_id: str,
    actor_role: UserRole,
    occurred_at: datetime,
    correlation_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditRecord:
    """Insert an immutable audit event within the caller's active transaction."""
    row = AuditEventRow(
        id=str(uuid.uuid4()),
        request_id=request_id,
        event_type=event_type.value,
        actor_id=actor_id,
        actor_role=actor_role.value,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        metadata_json=metadata or {},
    )
    session.add(row)
    session.flush()
    return _to_audit_record(row)


def list_audit_events_for_request(session: Session, request_id: str) -> list[AuditRecord]:
    stmt = (
        select(AuditEventRow)
        .where(AuditEventRow.request_id == request_id)
        .order_by(AuditEventRow.occurred_at.asc(), AuditEventRow.id.asc())
    )
    return [_to_audit_record(row) for row in session.scalars(stmt).all()]


def _to_audit_record(row: AuditEventRow) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        request_id=row.request_id,
        event_type=AuditEventType(row.event_type),
        actor_id=row.actor_id,
        actor_role=UserRole(row.actor_role),
        occurred_at=row.occurred_at,
        correlation_id=row.correlation_id,
        metadata=dict(row.metadata_json or {}),
    )
