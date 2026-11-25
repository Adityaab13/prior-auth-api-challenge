"""SQLAlchemy 2.0 ORM tables and DB-level integrity constraints.

ORM models are strictly confined to the persistence layer (db/ and repos/).
The domain and API layers operate exclusively on immutable dataclasses and Pydantic DTOs.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PracticeRow(Base):
    __tablename__ = "practices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('member', 'provider_office', 'reviewer', 'admin')",
            name="ck_users_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    practice_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("practices.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MemberRow(Base):
    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dob: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MemberCoverageRow(Base):
    __tablename__ = "member_coverages"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'terminated')", name="ck_member_coverages_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_member_coverages_dates",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    member_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("members.id"), nullable=False, index=True
    )
    plan_name: Mapped[str] = mapped_column(String(200), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class PracticePatientRow(Base):
    __tablename__ = "practice_patients"

    practice_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("practices.id"), primary_key=True
    )
    member_id: Mapped[str] = mapped_column(String(64), ForeignKey("members.id"), primary_key=True)


class ClinicianPatientRow(Base):
    """Links treating clinicians (who may also serve as reviewers) to their patients for COI."""

    __tablename__ = "clinician_patients"

    clinician_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), primary_key=True
    )
    member_id: Mapped[str] = mapped_column(String(64), ForeignKey("members.id"), primary_key=True)


class PriorAuthRequestRow(Base):
    __tablename__ = "prior_auth_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted', 'in_review', 'approved', 'partially_approved', 'denied')",
            name="ck_prior_auth_status",
        ),
        CheckConstraint(
            "(status IN ('submitted', 'in_review')) OR "
            "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_prior_auth_decided_attribution",
        ),
        CheckConstraint(
            "(status != 'in_review') OR (assigned_reviewer_id IS NOT NULL)",
            name="ck_prior_auth_in_review_assigned",
        ),
        Index(
            "uq_prior_auth_practice_idempotency",
            "practice_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    member_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("members.id"), nullable=False, index=True
    )
    practice_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("practices.id"), nullable=False, index=True
    )
    submitted_by: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    diagnosis_code: Mapped[str] = mapped_column(String(16), nullable=False)
    date_of_service: Mapped[date] = mapped_column(Date, nullable=False)
    clinical_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    assigned_reviewer_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True, index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    procedures: Mapped[list[PriorAuthProcedureRow]] = relationship(
        "PriorAuthProcedureRow",
        back_populates="request",
        order_by="PriorAuthProcedureRow.id",
        cascade="all, delete-orphan",
    )


class PriorAuthProcedureRow(Base):
    __tablename__ = "prior_auth_procedures"
    __table_args__ = (
        CheckConstraint("code_type IN ('CPT', 'HCPCS')", name="ck_proc_code_type"),
        CheckConstraint("units > 0 AND units <= 999", name="ck_proc_units_bounds"),
        CheckConstraint("decision IN ('pending', 'approved', 'denied')", name="ck_proc_decision"),
        CheckConstraint(
            "(decision != 'denied') OR (denial_reason IS NOT NULL AND length(trim(denial_reason)) > 0)",
            name="ck_proc_denial_reason_required",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prior_auth_requests.id"), nullable=False, index=True
    )
    code_type: Mapped[str] = mapped_column(String(8), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    high_units_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped[PriorAuthRequestRow] = relationship(
        "PriorAuthRequestRow", back_populates="procedures"
    )


class AuditEventRow(Base):
    """Append-only compliance ledger. Protected by a Postgres BEFORE UPDATE/DELETE trigger."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prior_auth_requests.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SubmissionAttemptRow(Base):
    """PHI-free support desk record of accepted and rejected submission attempts."""

    __tablename__ = "submission_attempts"
    __table_args__ = (
        CheckConstraint("outcome IN ('accepted', 'rejected')", name="ck_submission_outcome"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    practice_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    prior_auth_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class NotificationRow(Base):
    """Transactional outbox for notifying provider offices when a decision is made."""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'delivered')", name="ck_notification_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prior_auth_requests.id"), nullable=False, index=True
    )
    practice_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("practices.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
