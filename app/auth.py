"""Real JWT creation and verification with role-scoped FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.domain.types import UserRole

bearer_scheme = HTTPBearer(
    scheme_name="BearerJWT",
    description=(
        "Signed JWT issued by the identity service containing `sub` (user ID), `role`, "
        "and optional `practice_id` (required for `provider_office` callers). "
        "In development mode, mint tokens via `POST /api/v1/dev/tokens`."
    ),
    auto_error=False,
)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    role: UserRole
    practice_id: str | None = None


def create_access_token(
    *,
    user_id: str,
    role: UserRole,
    practice_id: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    expires_minutes: int | None = None,
) -> str:
    cfg = settings or get_settings()
    issued_at = now or datetime.now(UTC)
    ttl = expires_minutes if expires_minutes is not None else cfg.jwt_expires_minutes
    payload: dict[str, object] = {
        "sub": user_id,
        "role": role.value,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(minutes=ttl)).timestamp()),
    }
    if practice_id is not None:
        payload["practice_id"] = practice_id
    return jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def decode_access_token(token: str, settings: Settings | None = None) -> AuthenticatedUser:
    cfg = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            cfg.jwt_secret,
            algorithms=[cfg.jwt_algorithm],
            options={"require": ["sub", "role", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_EXPIRED", "message": "Authentication token has expired."},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Authentication token is invalid."},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        role = UserRole(claims["role"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_ROLE_CLAIM", "message": "Token contains an unrecognized role."},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    practice_id = claims.get("practice_id")
    if role == UserRole.PROVIDER_OFFICE and not practice_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "MISSING_PRACTICE_CLAIM",
                "message": "Provider office tokens must include a practice_id claim.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedUser(
        user_id=str(claims["sub"]),
        role=role,
        practice_id=str(practice_id) if practice_id is not None else None,
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Provide a valid Bearer JWT in the Authorization header.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_access_token(credentials.credentials, settings=settings)


def require_roles(*allowed_roles: UserRole) -> Callable[[AuthenticatedUser], AuthenticatedUser]:
    allowed = frozenset(allowed_roles)

    def _dependency(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "INSUFFICIENT_ROLE",
                    "message": "Your role is not permitted to access this endpoint.",
                },
            )
        return user

    return _dependency
