"""Audit log. Append-only (trigger in the initial migration), like the ledger."""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import id_field, json_field, utcnow


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    __table_args__ = (sa.Index(None, "target_type", "target_id"),)

    id: uuid.UUID = id_field()
    actor_id: uuid.UUID | None = Field(default=None, foreign_key="users.id", index=True)  # NULL = system
    action: str = Field(max_length=100)  # e.g. "access_request.approve", "key.issue", "pod.stop"
    target_type: str = Field(max_length=50)  # e.g. "access_request", "issued_resource"
    target_id: str = Field(max_length=100)  # str so composite or external ids fit too
    details: dict[str, Any] = json_field()
    at: datetime = Field(
        default_factory=utcnow,
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"server_default": sa.func.now()},
        nullable=False,
        index=True,
    )
