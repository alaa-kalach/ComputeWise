"""Shared test setup.

Tests run against a SEPARATE Postgres database given by TEST_DATABASE_URL (in .env or the
environment). They rebuild its schema with the real Alembic migration, load the seed, and
roll back every test, so tests never see each other's changes.

NEVER point TEST_DATABASE_URL at the shared team database: the schema is dropped and
recreated. The fixture refuses to run if it equals DATABASE_URL.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from dotenv import dotenv_values

BACKEND = Path(__file__).resolve().parents[1]
_dotenv = {**dotenv_values(BACKEND.parent / ".env"), **dotenv_values(BACKEND / ".env")}
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _dotenv.get("TEST_DATABASE_URL")
SHARED_DATABASE_URL = os.environ.get("DATABASE_URL") or _dotenv.get("DATABASE_URL")

if not TEST_DATABASE_URL:
    pytest.exit(
        "TEST_DATABASE_URL is not set. Add a separate test database to .env (see .env.example).",
        returncode=4,
    )
if SHARED_DATABASE_URL and TEST_DATABASE_URL.strip() == SHARED_DATABASE_URL.strip():
    pytest.exit("TEST_DATABASE_URL must not be the shared DATABASE_URL: tests wipe the schema.", returncode=4)

# Settings for the app under test (environment variables override .env).
TEST_SUPABASE_URL = "https://test-project.supabase.co"
TEST_JWT_SECRET = "test-only-secret-at-least-32-characters-long!!"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SUPABASE_URL"] = TEST_SUPABASE_URL
os.environ["SUPABASE_JWT_SECRET"] = TEST_JWT_SECRET
os.environ.setdefault("FERNET_KEY", "Zm9yLXRlc3RzLW9ubHktMzItYnl0ZXMta2V5LTEyMzQ=")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from scripts.seed import insert_seed, seed_id  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = create_engine(get_settings().database_url)
    with Session(eng) as s:
        insert_seed(s, now=datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc))
        s.commit()
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose changes are rolled back after the test (commits included)."""
    conn = engine.connect()
    outer = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        s.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_token(
    email: str,
    *,
    sub: uuid.UUID | None = None,
    secret: str = TEST_JWT_SECRET,
    expires_in: timedelta = timedelta(hours=1),
    audience: str = "authenticated",
    issuer: str = f"{TEST_SUPABASE_URL}/auth/v1",
    **extra: object,
) -> str:
    """A Supabase-shaped access token signed with the test secret."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(sub or uuid.uuid5(uuid.NAMESPACE_DNS, email)),
        "email": email,
        "aud": audience,
        "iss": issuer,
        "role": "authenticated",
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        **extra,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


EMAILS = {
    "dana": "dana.haddad@cs.example.edu",  # department admin
    "karim": "karim.nassar@cs.example.edu",  # Prof A: FYP Team 1, FYP Team 2
    "rania": "rania.khoury@cs.example.edu",  # Prof B: Research Lab
    "sara": "sara.mansour@students.example.edu",  # FYP Team 1 AND Research Lab
    "omar": "omar.itani@students.example.edu",  # FYP Team 1
    "lina": "lina.saleh@students.example.edu",  # FYP Team 2
    "maya": "maya.aoun@students.example.edu",  # Research Lab
}


def auth(who: str) -> dict[str, str]:
    """Headers for a request as a seed person: client.get(url, headers=auth('karim'))."""
    return {"Authorization": f"Bearer {make_token(EMAILS[who])}"}


__all__ = ["EMAILS", "auth", "make_token", "seed_id"]
