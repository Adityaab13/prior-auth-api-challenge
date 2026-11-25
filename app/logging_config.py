"""PHI-safe structured JSON logging with request correlation IDs."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

# Explicit allowlist of non-PHI extra fields that may appear in structured logs.
_ALLOWED_LOG_FIELDS = frozenset(
    {
        "event",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "actor_id",
        "actor_role",
        "practice_id",
        "prior_auth_id",
        "outcome",
        "error_codes",
    }
)


class PhiSafeJsonFormatter(logging.Formatter):
    """Formats logs as single-line JSON and drops any non-allowlisted extra attributes."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_ctx.get(),
            "message": record.getMessage(),
        }
        for key in _ALLOWED_LOG_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(PhiSafeJsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Quiet noisy third-party loggers that might echo SQL parameters containing PHI.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
