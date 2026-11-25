"""Development-only helper router to mint real signed JWTs for seeded users in Swagger."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.domain.types import UserRole
from app.repos import reference_repo

router = APIRouter(
    prefix="/api/v1/dev",
    tags=["Dev Auth Helper (Development Only)"],
)


class DevUserView(BaseModel):
    user_id: str
    role: UserRole
    display_name: str
    practice_id: str | None


class MintTokenRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"user_id": "user-prov-maine"},
                {"user_id": "user-rev-nurse"},
                {"user_id": "user-mem-alice"},
                {"user_id": "user-admin-1"},
            ]
        }
    )

    user_id: str = Field(..., description="Seeded user ID to mint a signed Bearer JWT for.")


class MintTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    role: UserRole
    practice_id: str | None


@router.get(
    "/users",
    response_model=list[DevUserView],
    summary="List seeded synthetic users",
    description="Development-only endpoint listing seeded users you can mint JWTs for in Swagger.",
)
def list_seeded_users(session: Session = Depends(get_db_session)) -> list[DevUserView]:
    rows = reference_repo.list_all_users(session)
    return [
        DevUserView(
            user_id=u.id,
            role=UserRole(u.role),
            display_name=u.display_name,
            practice_id=u.practice_id,
        )
        for u in rows
    ]


@router.post(
    "/tokens",
    response_model=MintTokenResponse,
    summary="Mint a signed JWT for a seeded user",
    description=(
        "Development-only endpoint that signs a real HS256 JWT for any seeded user. "
        "Copy `access_token` into Swagger's **Authorize** dialog."
    ),
)
def mint_dev_token(
    payload: MintTokenRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> MintTokenResponse:
    user = reference_repo.get_user_by_id(session, payload.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "USER_NOT_FOUND",
                "message": f"Seeded user '{payload.user_id}' was not found.",
            },
        )

    role = UserRole(user.role)
    token = create_access_token(
        user_id=user.id,
        role=role,
        practice_id=user.practice_id,
        settings=settings,
    )
    return MintTokenResponse(
        access_token=token,
        user_id=user.id,
        role=role,
        practice_id=user.practice_id,
    )
