"""Shared building blocks for every table: enums, column helpers, naming convention."""

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Predictable constraint names, so Alembic migrations are stable across machines.
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# ---------------------------------------------------------------- enums
class Provider(str, enum.Enum):
    openrouter = "openrouter"
    runpod = "runpod"


class OwnerType(str, enum.Enum):
    department = "department"
    professor = "professor"


class ScopeType(str, enum.Enum):
    professor = "professor"
    group = "group"
    user = "user"


class LedgerType(str, enum.Enum):
    allocation = "allocation"
    spend = "spend"
    rollover = "rollover"
    adjustment = "adjustment"


class CredentialStatus(str, enum.Enum):
    active = "active"
    revoked = "revoked"


class GrantStatus(str, enum.Enum):
    active = "active"
    revoked = "revoked"


class MemberStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class RequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ResourceStatus(str, enum.Enum):
    # OpenRouter keys
    active = "active"
    capped = "capped"
    disabled = "disabled"
    # RunPod pods
    running = "running"
    stopped = "stopped"
    terminated = "terminated"


# ---------------------------------------------------------------- column helpers
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_type(e: type[enum.Enum]) -> sa.Enum:
    """Stored as VARCHAR + CHECK (not a native Postgres enum), so adding a value later
    is a simple migration and downgrades don't leave orphan enum types behind."""
    return sa.Enum(
        e,
        name=e.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


MONEY = sa.Numeric(12, 4)  # USD; always Decimal in Python, never float


def id_field() -> Any:
    return Field(default_factory=uuid.uuid4, primary_key=True)


def created_at_field() -> Any:
    return Field(
        default_factory=utcnow,
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"server_default": sa.func.now()},
        nullable=False,
    )


def money_field(*, nullable: bool = False, **kw: Any) -> Any:
    return Field(sa_type=MONEY, nullable=nullable, **kw)


def json_field() -> Any:
    return Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'{}'::jsonb")},
        nullable=False,
    )


__all__ = [
    "Decimal",
    "Field",
    "SQLModel",
    "uuid",
    "datetime",
]
