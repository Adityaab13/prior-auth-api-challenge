"""Initial prior authorization schema with compliance constraints and append-only audit trigger.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "practices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("practice_id", sa.String(64), sa.ForeignKey("practices.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('member', 'provider_office', 'reviewer', 'admin')",
            name="ck_users_role",
        ),
    )

    op.create_table(
        "members",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), unique=True, nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("dob", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "member_coverages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("member_id", sa.String(64), sa.ForeignKey("members.id"), nullable=False),
        sa.Column("plan_name", sa.String(200), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint("status IN ('active', 'terminated')", name="ck_member_coverages_status"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_member_coverages_dates",
        ),
    )
    op.create_index("ix_member_coverages_member_id", "member_coverages", ["member_id"])

    op.create_table(
        "practice_patients",
        sa.Column("practice_id", sa.String(64), sa.ForeignKey("practices.id"), primary_key=True),
        sa.Column("member_id", sa.String(64), sa.ForeignKey("members.id"), primary_key=True),
    )

    op.create_table(
        "clinician_patients",
        sa.Column("clinician_user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("member_id", sa.String(64), sa.ForeignKey("members.id"), primary_key=True),
    )

    op.create_table(
        "prior_auth_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("member_id", sa.String(64), sa.ForeignKey("members.id"), nullable=False),
        sa.Column("practice_id", sa.String(64), sa.ForeignKey("practices.id"), nullable=False),
        sa.Column("submitted_by", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("diagnosis_code", sa.String(16), nullable=False),
        sa.Column("date_of_service", sa.Date(), nullable=False),
        sa.Column("clinical_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "assigned_reviewer_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('submitted', 'in_review', 'approved', 'partially_approved', 'denied')",
            name="ck_prior_auth_status",
        ),
        sa.CheckConstraint(
            "(status IN ('submitted', 'in_review')) OR "
            "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_prior_auth_decided_attribution",
        ),
        sa.CheckConstraint(
            "(status != 'in_review') OR (assigned_reviewer_id IS NOT NULL)",
            name="ck_prior_auth_in_review_assigned",
        ),
    )
    op.create_index("ix_prior_auth_requests_member_id", "prior_auth_requests", ["member_id"])
    op.create_index("ix_prior_auth_requests_practice_id", "prior_auth_requests", ["practice_id"])
    op.create_index("ix_prior_auth_requests_status", "prior_auth_requests", ["status"])
    op.create_index(
        "ix_prior_auth_requests_assigned_reviewer_id",
        "prior_auth_requests",
        ["assigned_reviewer_id"],
    )
    op.create_index(
        "uq_prior_auth_practice_idempotency",
        "prior_auth_requests",
        ["practice_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "prior_auth_procedures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("prior_auth_requests.id"),
            nullable=False,
        ),
        sa.Column("code_type", sa.String(8), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column(
            "high_units_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("code_type IN ('CPT', 'HCPCS')", name="ck_proc_code_type"),
        sa.CheckConstraint("units > 0 AND units <= 999", name="ck_proc_units_bounds"),
        sa.CheckConstraint("decision IN ('pending', 'approved', 'denied')", name="ck_proc_decision"),
        sa.CheckConstraint(
            "(decision != 'denied') OR (denial_reason IS NOT NULL AND length(trim(denial_reason)) > 0)",
            name="ck_proc_denial_reason_required",
        ),
    )
    op.create_index("ix_prior_auth_procedures_request_id", "prior_auth_procedures", ["request_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("prior_auth_requests.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])

    op.create_table(
        "submission_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("practice_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_codes", sa.JSON(), nullable=False),
        sa.Column("prior_auth_id", sa.String(36), nullable=True),
        sa.CheckConstraint("outcome IN ('accepted', 'rejected')", name="ck_submission_outcome"),
    )
    op.create_index("ix_submission_attempts_correlation_id", "submission_attempts", ["correlation_id"])
    op.create_index("ix_submission_attempts_practice_id", "submission_attempts", ["practice_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("prior_auth_requests.id"),
            nullable=False,
        ),
        sa.Column("practice_id", sa.String(64), sa.ForeignKey("practices.id"), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'delivered')", name="ck_notification_status"),
    )
    op.create_index("ix_notifications_request_id", "notifications", ["request_id"])
    op.create_index("ix_notifications_practice_id", "notifications", ["practice_id"])

    # Enforce append-only compliance ledger at the PostgreSQL level.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only and cannot be modified or deleted';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_events_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_audit_event_mutation();
        """
    )

    # Enforce immutability of finalized prior authorization decisions at the PostgreSQL level.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_final_decision_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.status IN ('approved', 'partially_approved', 'denied') THEN
                RAISE EXCEPTION 'Finalized prior authorization decisions are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prior_auth_final_immutable
        BEFORE UPDATE OR DELETE ON prior_auth_requests
        FOR EACH ROW
        EXECUTE FUNCTION prevent_final_decision_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_prior_auth_final_immutable ON prior_auth_requests;")
    op.execute("DROP FUNCTION IF EXISTS prevent_final_decision_mutation();")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events;")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_event_mutation();")
    op.drop_table("notifications")
    op.drop_table("submission_attempts")
    op.drop_table("audit_events")
    op.drop_table("prior_auth_procedures")
    op.drop_table("prior_auth_requests")
    op.drop_table("clinician_patients")
    op.drop_table("practice_patients")
    op.drop_table("member_coverages")
    op.drop_table("members")
    op.drop_table("users")
    op.drop_table("practices")
