"""Shared pytest fixtures supporting PostgreSQL (via TEST_DATABASE_URL) or in-memory SQLite."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import create_access_token
from app.config import Settings
from app.db.models import Base
from app.db.seed import seed_database
from app.db.session import get_db_session
from app.domain.types import UserRole
from app.main import create_app


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        database_url=os.getenv("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:"),
        jwt_secret="test-secret-key-at-least-32-bytes-long",
        jwt_algorithm="HS256",
        jwt_expires_minutes=60,
        env="test",
        log_level="WARNING",
    )


@pytest.fixture()
def db_engine(test_settings: Settings) -> Generator[Engine, None, None]:
    url = test_settings.database_url
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    else:
        engine = create_engine(url, pool_pre_ping=True, future=True)

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as seed_session:
        seed_database(seed_session)

    yield engine

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(
    db_engine: Engine, test_settings: Settings
) -> Generator[TestClient, None, None]:
    from app.config import get_settings

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def _override_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app = create_app(settings=test_settings)
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_settings] = lambda: test_settings

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(test_settings: Settings):
    def _make(
        user_id: str, role: UserRole, practice_id: str | None = None
    ) -> dict[str, str]:
        token = create_access_token(
            user_id=user_id,
            role=role,
            practice_id=practice_id,
            settings=test_settings,
        )
        return {"Authorization": f"Bearer {token}"}

    return _make
