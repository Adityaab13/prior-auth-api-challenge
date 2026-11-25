# Oxbridge Health — Prior Authorization API

Backend service for submitting, reviewing, deciding, and auditing prior authorization requests, built with **FastAPI**, **SQLAlchemy 2.0**, **Alembic**, **PostgreSQL**, and **real signed JWTs (PyJWT)**.

The codebase follows **Functional Core, Imperative Shell (FCIS)** and **KISS**:
- **Functional Core (`app/domain/`)**: Pure functions + immutable frozen dataclasses with zero I/O or system-clock calls. Every business rule is unit-tested in milliseconds.
- **Imperative Shell (`app/repos/`, `app/services/`)**: Plain functions orchestrating explicit PostgreSQL transactions, `FOR UPDATE SKIP LOCKED` queue claiming, and atomic writes to the append-only audit ledger.
- **Persona-Separated API (`app/api/`)**: Dedicated routers and response DTOs per role (`provider`, `member`, `reviewer`, `admin`) so each roadmap evolves independently and adding new roles (like `auditor`) is purely additive.

---

## How to Run It

### 1. Single-command startup (Docker Compose)
```bash
docker compose up --build
```
On startup, the API container automatically:
1. Waits for PostgreSQL 16 to pass its health check.
2. Runs Alembic migrations (`alembic upgrade head`), including DB `CHECK` constraints and the append-only `audit_events` trigger.
3. Applies idempotent synthetic seed data (`python -m app.db.seed`).
4. Starts the API at `http://localhost:8000`.

### 2. Explore the Portal & Partner API Docs
- **Prior Authorization Portal:** `http://localhost:8000/` *(development-only zero-build user-facing portal with account switching across Provider Office, Reviewer, Member, and Operations Admin, plus a confirmation popup when procedure units exceed 30)*
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`
- **OpenAPI JSON:** `http://localhost:8000/openapi.json`
- **Health Check:** `http://localhost:8000/health`

#### Trying Endpoints in Swagger (Development Mode)
In development (`ENV=development`), a dev helper router is mounted so you can mint real HS256 JWTs for any seeded user without building login screens:
1. Call `GET /api/v1/dev/users` to see the seeded accounts.
2. Call `POST /api/v1/dev/tokens` with e.g. `{"user_id": "user-prov-maine"}`.
3. Click **Authorize** in Swagger UI and paste the returned `access_token`.

Or print tokens for all seeded accounts from the CLI:
```bash
uv run python -m scripts.mint_token
```

#### Seeded Synthetic Scenarios
| User ID | Role | Practice / Notes |
|---|---|---|
| `user-prov-maine` | `provider_office` | `prac-maine` (`America/New_York`) — linked to `mem-alice` (active) and `mem-carol` (terminated coverage) |
| `user-prov-hawaii` | `provider_office` | `prac-hawaii` (`Pacific/Honolulu`) — linked to `mem-bob` (active) |
| `user-mem-alice` | `member` | `mem-alice` — treated by `user-rev-dr-smith` (conflict-of-interest scenario) |
| `user-mem-bob` | `member` | `mem-bob` — active open-ended coverage |
| `user-mem-carol` | `member` | `mem-carol` — terminated coverage (triggers `COVERAGE_INACTIVE`) |
| `user-rev-nurse` | `reviewer` | Jordan Lee, RN — no patient conflicts |
| `user-rev-dr-smith` | `reviewer` | Dr. Morgan Smith, MD — treats `mem-alice` (blocked from claiming/reviewing Alice's requests) |
| `user-admin-1` | `admin` | Full visibility, reassignment, audit trail, workload summary, support desk lookup |

### 3. Running Tests & Linting Locally
```bash
uv sync --extra dev
uv run pytest -v
uv run ruff check app tests scripts
```
By default, integration tests run against an isolated in-memory SQLite database with foreign keys enabled for fast feedback. To run the full integration suite directly against PostgreSQL:
```bash
TEST_DATABASE_URL=postgresql+psycopg://prior_auth:prior_auth@localhost:5432/prior_auth uv run pytest -v
```

---

## API Overview

| Persona | Endpoints |
|---|---|
| **Provider Office** | `POST /api/v1/provider/requests` (supports `Idempotency-Key`), `GET /api/v1/provider/requests`, `GET /api/v1/provider/requests/{id}`, `GET /api/v1/provider/notifications` |
| **Member** | `GET /api/v1/member/requests`, `GET /api/v1/member/requests/{id}` *(clinical notes & denial reasons structurally excluded)* |
| **Reviewer** | `GET /api/v1/reviewer/queue`, `POST /api/v1/reviewer/queue/claim-next`, `POST /api/v1/reviewer/requests/{id}/claim`, `GET /api/v1/reviewer/requests`, `GET /api/v1/reviewer/requests/{id}`, `POST /api/v1/reviewer/requests/{id}/decision` |
| **Operations Admin** | `GET /api/v1/admin/requests`, `GET /api/v1/admin/requests/{id}` (includes full audit trail), `POST /api/v1/admin/requests/{id}/reassign`, `GET /api/v1/admin/reviewers/workload`, `GET /api/v1/admin/support/submissions` |

---

## Decisions and Assumptions

1. **Nationwide Offices (Maine to Hawaii) & Date-of-Service Window**
   - *Ambiguity:* "procedures that have already happened" and "more than a year out" depend on what calendar date "today" is. Using UTC would reject legitimate same-day requests from Hawaii (`UTC-10`) after 2:00 PM local time.
   - *Decision:* Each practice stores an IANA timezone (`America/New_York`, `Pacific/Honolulu`, etc.). `date_of_service` is compared against the practice's local calendar date (`practice_local_today`). One year out uses calendar-year addition with Feb 29 clamped to Feb 28.

2. **"More than 30 units is almost always a typo"**
   - *Ambiguity:* "Almost always" implies legitimate high-unit procedures exist, so a hard rejection at 30 would block valid clinical requests.
   - *Decision:* Soft-block by default (`422 HIGH_UNITS_CONFIRMATION_REQUIRED`) telling the office which procedure exceeded 30 units and how to confirm if intentional (`confirm_high_units: true`), alongside a hard ceiling of 999 units.

3. **Clinical Notes & Reviewer Reasoning Visibility (Member vs. Provider Office)**
   - *Decision:* Members see their requests and per-procedure approval outcomes (`pending` / `approved` / `denied`), and **never** see `clinical_note` or `denial_reason`. Those fields do not exist on `MemberRequestView` / `MemberProcedureView`, making accidental serialization impossible. Provider offices see their own submitted `clinical_note` and procedure `denial_reason` so they understand why a procedure was denied.

4. **Preventing Unlinked Patient / Coverage Enumeration (HIPAA Minimum Necessary)**
   - *Decision:* If a provider office submits for a member ID that does not exist or is not linked to their practice, we return `PATIENT_NOT_LINKED` and skip coverage evaluation so callers cannot probe coverage dates for patients outside their practice.

5. **Conflict of Interest (COI) for Physician-Reviewers**
   - *Decision:* Enforced in every reviewer and admin touchpoint: `GET /reviewer/queue` and `POST /reviewer/queue/claim-next` automatically exclude patients the reviewer treats, `POST /reviewer/requests/{id}/claim` returns `403 CONFLICT_OF_INTEREST`, and `POST /admin/requests/{id}/reassign` refuses to assign a request to a treating physician (`409`). Decision submission also re-checks COI as defense-in-depth.

6. **Concurrency & Atomic Per-Procedure Decisions**
   - *Decision:* Queue pickup uses PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` so concurrent reviewers never claim the same request. Reviewers submit decisions for every procedure in one atomic request; the overall status is derived purely from procedure outcomes (`approved`, `partially_approved`, or `denied`). Once decided, the request is immutable at both the application layer and PostgreSQL trigger level (`trg_prior_auth_final_immutable`).

7. **Compliance Audit Guarantee ("Never approved with no record of who approved it")**
   - *Decision:* Enforced at **three** layers:
     1. Every state transition (`submitted`, `claimed`, `reassigned`, `decided`) inserts an `audit_events` row inside the **same database transaction**.
     2. PostgreSQL enforces a `CHECK` constraint requiring `decided_by IS NOT NULL AND decided_at IS NOT NULL` for any final status, and a `CHECK` constraint requiring non-empty `denial_reason` on any denied procedure.
     3. PostgreSQL triggers make `audit_events` strictly append-only (`BEFORE UPDATE OR DELETE`) and block any mutation of finalized `prior_auth_requests` rows.

8. **HIPAA & Support Desk Traceability Without PHI Leakage**
   - *Decision:* Every HTTP request carries an `X-Request-ID` correlation header (echoed in headers and error bodies), and all `/api/*` responses include `Cache-Control: no-store`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff` so proxies/browsers never cache PHI. Structured logs use an explicit non-PHI allowlist (`request_id`, `actor_id`, `practice_id`, `prior_auth_id`, `error_codes`, timings). Both domain-level and schema-level (`422`) submission rejections are written to the PHI-free `submission_attempts` table queryable via `GET /api/v1/admin/support/submissions`.

9. **Partner Integrations & Independent Persona Roadmaps (Adding `auditor` Next)**
   - *Decision:* All validation errors (both Pydantic schema errors and domain rules) share a single `ErrorResponse` envelope returning **all** detected issues at once. `POST /api/v1/provider/requests` supports an `Idempotency-Key` header per practice so partner retries never create duplicate authorizations. Provider offices are scoped by `practice_id` so multiple intake staff in the same office see their practice's requests. Because each persona has its own router and DTOs, adding an `auditor` role next is purely additive (`UserRole.AUDITOR` + new read-only `app/api/auditor.py` router) without touching existing personas.

10. **Wishlist Items Included**
    - **Office Decision Notifications:** Implemented via a transactional outbox table (`notifications`) written atomically when a decision is made, exposed via `GET /api/v1/provider/notifications`.
    - **Reviewer Workload:** Implemented via `GET /api/v1/admin/reviewers/workload` showing open (`in_review`) counts per reviewer.

---

## How AI Was Used

- **What I directed AI to do:** We first aligned on architecture (Functional Core / Imperative Shell, KISS, SQLAlchemy 2.0 strictly confined to `db/` and `repos/`, real HS256 JWTs, role-isolated FastAPI routers with Swagger docs, and no custom UI). AI then scaffolded the project, Alembic migration, pure domain rules, repositories, services, persona routers, and unit/integration tests step by step.
- **Issues caught and fixed during build/testing:**
  1. **Frozen `Exception` dataclass bug:** AI initially annotated `DomainError(Exception)` with `@dataclass(frozen=True)`. Integration tests immediately failed with `FrozenInstanceError: cannot assign to field '__traceback__'` when FastAPI propagated `DomainError` through its `yield` DB session dependency. We converted `DomainError` to a standard `Exception` subclass while keeping all value objects frozen.
  2. **Seed FK flush ordering:** In `app/db/seed.py`, calling `session.merge()` for all tables before a single `session.commit()` caused SQLAlchemy's unit-of-work to flush `clinician_patients` before parent rows in `users`/`members`, triggering a foreign-key `IntegrityError`. Adding explicit `session.flush()` boundaries between parent and child seed batches resolved it cleanly.
  3. **Ruff lint configuration for FastAPI:** FastAPI's idiomatic `Depends()`/`Query()`/`Header()` defaults tripped `B008`; we configured `extend-immutable-calls` in `pyproject.toml` and modernized `timezone.utc` to `datetime.UTC`.
  4. **Alembic logging config on Docker startup:** Running `docker compose up` caught a missing `qualname` entry in `alembic.ini` for `logger_sqlalchemy` and `logger_alembic`; we added both and re-verified clean startup against PostgreSQL 16.
