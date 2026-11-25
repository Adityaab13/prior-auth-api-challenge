"""Immutable domain types, enums, and functional error structures (zero I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class UserRole(StrEnum):
    MEMBER = "member"
    PROVIDER_OFFICE = "provider_office"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class CodeType(StrEnum):
    CPT = "CPT"
    HCPCS = "HCPCS"


class RequestStatus(StrEnum):
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    DENIED = "denied"

    @property
    def is_final(self) -> bool:
        return self in {
            RequestStatus.APPROVED,
            RequestStatus.PARTIALLY_APPROVED,
            RequestStatus.DENIED,
        }


class ProcedureDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class CoverageStatus(StrEnum):
    ACTIVE = "active"
    TERMINATED = "terminated"


class AuditEventType(StrEnum):
    REQUEST_SUBMITTED = "request_submitted"
    REQUEST_CLAIMED = "request_claimed"
    REQUEST_REASSIGNED = "request_reassigned"
    REQUEST_DECIDED = "request_decided"


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    message: str


class DomainError(Exception):
    """Structured domain error mapped cleanly to HTTP status codes in the API shell.

    Note: Not `@dataclass(frozen=True)` because Python mutates `__traceback__` when
    propagating exceptions across generator dependencies (e.g. FastAPI `yield`).
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        issues: tuple[ValidationIssue, ...] = (),
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.issues = issues


@dataclass(frozen=True)
class CoverageRecord:
    coverage_id: str
    member_id: str
    plan_name: str
    effective_from: date
    effective_to: date | None
    status: CoverageStatus


@dataclass(frozen=True)
class ProcedureInput:
    code_type: CodeType
    code: str
    units: int
    confirm_high_units: bool = False


@dataclass(frozen=True)
class SubmissionCommand:
    member_id: str
    diagnosis_code: str
    date_of_service: date
    procedures: tuple[ProcedureInput, ...]
    clinical_note: str | None = None


@dataclass(frozen=True)
class SubmissionContext:
    """All external facts loaded by the imperative shell before running pure validation."""

    practice_id: str
    practice_timezone: str
    member_exists: bool
    is_patient_linked_to_practice: bool
    coverages: tuple[CoverageRecord, ...]
    now_utc: datetime


@dataclass(frozen=True)
class ValidatedProcedure:
    code_type: CodeType
    code: str
    units: int
    high_units_confirmed: bool


@dataclass(frozen=True)
class ValidatedSubmission:
    member_id: str
    practice_id: str
    diagnosis_code: str
    date_of_service: date
    procedures: tuple[ValidatedProcedure, ...]
    clinical_note: str | None


@dataclass(frozen=True)
class PriorAuthProcedure:
    id: str
    request_id: str
    code_type: CodeType
    code: str
    units: int
    high_units_confirmed: bool
    decision: ProcedureDecision
    denial_reason: str | None


@dataclass(frozen=True)
class PriorAuthRequest:
    id: str
    member_id: str
    practice_id: str
    submitted_by: str
    diagnosis_code: str
    date_of_service: date
    clinical_note: str | None
    status: RequestStatus
    assigned_reviewer_id: str | None
    assigned_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
    procedures: tuple[PriorAuthProcedure, ...]


@dataclass(frozen=True)
class ProcedureDecisionInput:
    procedure_id: str
    decision: ProcedureDecision
    denial_reason: str | None = None


@dataclass(frozen=True)
class AuditRecord:
    id: str
    request_id: str
    event_type: AuditEventType
    actor_id: str
    actor_role: UserRole
    occurred_at: datetime
    correlation_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmissionAttemptRecord:
    id: str
    correlation_id: str
    practice_id: str
    actor_id: str
    occurred_at: datetime
    outcome: str
    error_codes: tuple[str, ...]
    prior_auth_id: str | None


@dataclass(frozen=True)
class NotificationRecord:
    id: str
    request_id: str
    practice_id: str
    event_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime


@dataclass(frozen=True)
class ReviewerWorkload:
    reviewer_id: str
    display_name: str
    open_request_count: int
