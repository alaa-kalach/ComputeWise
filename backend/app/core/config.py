"""Settings read from the environment (and from a .env file at the repo root or in backend/)."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),  # repo root first, backend/.env overrides
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres connection string. Supabase gives "postgresql://..."; it is normalised
    # to the psycopg (v3) driver below so you can paste it as-is.
    database_url: str

    # Fernet key for encrypting provider secrets at rest.
    # Generate one with: python -m app.core.crypto
    fernet_key: str = ""

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, v: str) -> str:
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
