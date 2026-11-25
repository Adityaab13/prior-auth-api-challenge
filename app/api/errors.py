"""Consistent error schemas and FastAPI exception handlers with X-Request-ID."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.auth import decode_access_token
from app.config import get_settings
from app.db.session import get_db_session
from app.domain.types import DomainError, UserRole
from app.logging_config import request_id_ctx
from app.repos import support_repo

logger = logging.getLogger(__name__)


class ErrorDetailItem(BaseModel):
    field: str = Field(..., description="Dot/bracket path to the field that failed validation.")
    code: str = Field(..., description="Stable machine-readable error code for partner integrations.")
    message: str = Field(..., description="Human-readable guidance on exactly how to fix the issue.")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "8e1f0c4a-91b7-4c32-9a14-2b4e5a9d0b11",
                    "code": "VALIDATION_FAILED",
                    "message": "Submission could not be accepted. Fix the listed issues and resubmit.",
                    "errors": [
                        {
                            "field": "procedures[0].units",
                            "code": "HIGH_UNITS_CONFIRMATION_REQUIRED",
                            "message": (
                                "Units (45) exceed 30, which is usually a typo. Verify the "
                                "quantity or resubmit with procedures[0].confirm_high_units=true "
                                "if intentional."
                            ),
                        }
                    ],
                }
            ]
        }
    )

    request_id: str = Field(
        ...,
        description="Correlation ID echoed in X-Request-ID. Share with Oxbridge Support if needed.",
    )
    code: str = Field(..., description="Top-level machine-readable error code.")
    message: str = Field(..., description="Human-readable summary of the error.")
    errors: list[ErrorDetailItem] = Field(
        default_factory=list,
        description="Field-level issues (all detected issues are returned together).",
    )


COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid, or expired Bearer JWT."},
    403: {"model": ErrorResponse, "description": "Caller lacks role or has conflict of interest."},
    404: {"model": ErrorResponse, "description": "Requested resource not found or not accessible."},
    409: {"model": ErrorResponse, "description": "State conflict (e.g. already claimed or decided)."},
    422: {
        "model": ErrorResponse,
        "description": "Validation failed. All actionable field-level errors are listed in `errors`.",
    },
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        req_id = request_id_ctx.get()
        payload = ErrorResponse(
            request_id=req_id,
            code=exc.code,
            message=exc.message,
            errors=[
                ErrorDetailItem(field=i.field, code=i.code, message=i.message) for i in exc.issues
            ],
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(),
            headers={"X-Request-ID": req_id},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        req_id = request_id_ctx.get()
        if isinstance(exc.detail, dict):
            code = str(exc.detail.get("code", "HTTP_ERROR"))
            message = str(exc.detail.get("message", "Request failed."))
        else:
            code = "HTTP_ERROR"
            message = str(exc.detail)
        payload = ErrorResponse(request_id=req_id, code=code, message=message, errors=[])
        headers = dict(exc.headers or {})
        headers["X-Request-ID"] = req_id
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = request_id_ctx.get()
        items: list[ErrorDetailItem] = []
        for err in exc.errors():
            loc_parts = [str(part) for part in err.get("loc", ()) if part != "body"]
            field_path = ".".join(loc_parts) if loc_parts else "body"
            items.append(
                ErrorDetailItem(
                    field=field_path,
                    code=str(err.get("type", "INVALID_INPUT")).upper(),
                    message=str(err.get("msg", "Invalid field value.")),
                )
            )
        # Ensure support desk can also trace schema-level 422 submission rejections (PHI-free).
        if request.method == "POST" and request.url.path == "/api/v1/provider/requests":
            _record_schema_rejection_for_support(app, request, req_id, items)

        payload = ErrorResponse(
            request_id=req_id,
            code="INVALID_REQUEST_SCHEMA",
            message="Request payload did not match the expected schema.",
            errors=items,
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=payload.model_dump(),
            headers={"X-Request-ID": req_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
        req_id = request_id_ctx.get()
        # Never expose stack traces or potentially sensitive SQL/PHI details to callers.
        logger.error("Unhandled internal server error", extra={"event": "unhandled_exception"})
        payload = ErrorResponse(
            request_id=req_id,
            code="INTERNAL_ERROR",
            message=(
                "An unexpected error occurred. Please contact Oxbridge Support and provide "
                f"request_id '{req_id}'."
            ),
            errors=[],
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=payload.model_dump(),
            headers={"X-Request-ID": req_id},
        )


def _record_schema_rejection_for_support(
    app: FastAPI,
    request: Request,
    correlation_id: str,
    items: list[ErrorDetailItem],
) -> None:
    """Best-effort PHI-free support record when a provider submission fails JSON/schema parsing."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return
    token = auth_header.split(" ", 1)[1].strip()
    settings_provider = app.dependency_overrides.get(get_settings, get_settings)
    db_provider = app.dependency_overrides.get(get_db_session, get_db_session)
    try:
        user = decode_access_token(token, settings=settings_provider())
        if user.role != UserRole.PROVIDER_OFFICE or not user.practice_id:
            return
        error_codes = tuple(sorted({"INVALID_REQUEST_SCHEMA", *(item.code for item in items)}))
        session_gen = db_provider()
        session = next(session_gen)
        try:
            with session.begin():
                support_repo.record_submission_attempt(
                    session,
                    correlation_id=correlation_id,
                    practice_id=user.practice_id,
                    actor_id=user.user_id,
                    occurred_at=datetime.now(UTC),
                    outcome="rejected",
                    error_codes=error_codes,
                    prior_auth_id=None,
                )
        finally:
            try:
                next(session_gen)
            except StopIteration:
                pass
    except (HTTPException, SQLAlchemyError):
        logger.warning(
            "Unable to record schema rejection attempt for support",
            extra={"event": "support_record_skipped"},
        )
