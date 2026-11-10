"""Database engine and session dependency."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping: Supabase's pooler drops idle connections, so check before use.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: `session: Session = Depends(get_session)`."""
    with Session(get_engine()) as session:
        yield session
