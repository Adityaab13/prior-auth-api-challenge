from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://prior_auth:prior_auth@localhost:5432/prior_auth"
    jwt_secret: str = "dev-only-change-me-in-production-32-bytes-min"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 480
    env: str = "development"
    log_level: str = "INFO"

    @property
    def is_dev(self) -> bool:
        return self.env.lower() in {"development", "dev", "local", "test"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
