"""FastAPI application entrypoint, Swagger/OpenAPI metadata, and PHI-safe middleware."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import admin, dev, member, provider, reviewer
from app.api.errors import register_exception_handlers
from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.logging_config import configure_logging, request_id_ctx

API_DESCRIPTION = """
## Oxbridge Health — Prior Authorization API

Backend service for submitting, reviewing, deciding, and auditing prior authorization requests.

### Partner Integration Guide
1. **Authentication:** All `/api/v1/{persona}/*` endpoints require a signed Bearer JWT
   (`Authorization: Bearer <token>`) issued by Oxbridge's identity service containing `sub`,
   `role`, and (for `provider_office`) `practice_id`.
   - In development mode, call `GET /api/v1/dev/users` and `POST /api/v1/dev/tokens`, then click
     **Authorize** above and paste the returned `access_token`.
2. **Actionable Validation Errors (`422`):** Submissions are validated in a single pass. Every
   detected issue is returned in `errors: [{ field, code, message }]` so offices know everything
   to fix at once.
3. **Timezones:** `date_of_service` is evaluated against the submitting practice's local IANA
   timezone (supporting clinics nationwide from Maine to Hawaii). Requests cannot be in the past
   or more than 1 year in the future.
4. **High-Unit Typo Protection:** Any procedure with `units > 30` requires explicit confirmation
   (`confirm_high_units: true`).
5. **Idempotency:** Pass an `Idempotency-Key` header on `POST /api/v1/provider/requests` so
   retries after network timeouts return the original request (`200 OK`) instead of duplicating it.
6. **Support Correlation (`X-Request-ID`):** Every response includes an `X-Request-ID` header
   (and `request_id` in error bodies). Share this ID with Oxbridge Support to trace rejected
   submissions without exposing PHI.

### Common Machine-Readable Error Codes
| Code | HTTP | Meaning & Fix |
|---|---|---|
| `PATIENT_NOT_LINKED` | 422 | Member is not linked to your practice. |
| `DATE_OF_SERVICE_IN_PAST` | 422 | Service date is before your practice's local calendar date (retro review excluded). |
| `DATE_OF_SERVICE_TOO_FAR` | 422 | Service date is more than 1 year ahead of your practice's local date. |
| `COVERAGE_INACTIVE` | 422 | Member has no active coverage on `date_of_service`. |
| `INVALID_DIAGNOSIS_CODE` | 422 | `diagnosis_code` failed ICD-10-CM format validation. |
| `INVALID_PROCEDURE_CODE` | 422 | Procedure code failed CPT or HCPCS Level II format validation. |
| `HIGH_UNITS_CONFIRMATION_REQUIRED` | 422 | Procedure `units > 30` without `confirm_high_units: true`. |
| `CONFLICT_OF_INTEREST` | 403 / 409 | Reviewer treats this patient and cannot claim, be assigned, or decide it. |
| `REQUEST_ALREADY_DECIDED` | 409 | Decisions are final and immutable. |
"""

OPENAPI_TAGS = [
    {
        "name": "Provider Office",
        "description": "Submit prior authorizations for linked patients, view practice submissions, and read decision notifications.",
    },
    {
        "name": "Member",
        "description": "Patient-scoped view of prior authorization status and per-procedure outcomes (clinical notes and reviewer reasoning are never exposed).",
    },
    {
        "name": "Reviewer",
        "description": "Nurse and physician queue claiming (`SKIP LOCKED`), conflict-of-interest enforcement, and atomic final decisions.",
    },
    {
        "name": "Operations Admin",
        "description": "Full operational visibility, immutable audit trail inspection, reviewer reassignment, workload summary, and PHI-free support desk lookup.",
    },
    {
        "name": "Dev Auth Helper (Development Only)",
        "description": "Mounted only when `ENV=development` to mint signed JWTs for seeded synthetic accounts.",
    },
    {
        "name": "Health",
        "description": "Service and database readiness checks.",
    },
]


class HealthResponse(BaseModel):
    status: str
    database: str


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(cfg.log_level)
    logger = logging.getLogger("app.http")

    app = FastAPI(
        title="Oxbridge Health Prior Authorization API",
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        swagger_ui_parameters={"persistAuthorization": True},
    )

    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("X-Request-ID", "").strip()
        req_id = incoming[:64] if incoming else str(uuid.uuid4())
        token = request_id_ctx.set(req_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            if request.url.path.startswith("/api/"):
                # HIPAA defense-in-depth: prevent proxies/browsers from caching PHI responses.
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "HTTP request completed",
                extra={
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)

    app.include_router(provider.router)
    app.include_router(member.router)
    app.include_router(reviewer.router)
    app.include_router(admin.router)
    if cfg.is_dev:
        app.include_router(dev.router)
        explorer_html = Path(__file__).resolve().parent / "static" / "index.html"

        @app.get("/", include_in_schema=False)
        def dev_explorer() -> FileResponse:
            return FileResponse(explorer_html, media_type="text/html")

    @app.get("/health", response_model=HealthResponse, tags=["Health"], summary="Health check")
    def health(session: Session = Depends(get_db_session)) -> HealthResponse:
        session.execute(text("SELECT 1"))
        return HealthResponse(status="ok", database="ok")

    return app


app = create_app()
