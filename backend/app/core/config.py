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

    # ---- Supabase Auth (A2) ----------------------------------------------------
    # Project URL, e.g. https://abcd.supabase.co. Used to check who issued a token and
    # to fetch the public signing keys (JWKS) of new-style projects.
    supabase_url: str = ""
    # Only for projects that still sign tokens with the legacy shared secret (HS256),
    # and for tests. Leave empty for projects using the newer signing keys.
    supabase_jwt_secret: str = ""
    jwt_audience: str = "authenticated"

    # Frontend origins allowed to call the API from a browser (comma-separated).
    cors_origins: str = "http://localhost:5173"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, v: str) -> str:
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @field_validator("supabase_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
