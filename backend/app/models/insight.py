"""Agent escalations and team notifications (A6)."""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import created_at_field, id_field, json_field


class Escalation(SQLModel, table=True):
    """A proposal from the agent that a human approves or rejects. Never executed
    until approved. recipient_id is a professors.id or a departments.id, depending
    on recipient_type, so it has no foreign key."""

    __tablename__ = "escalations"
    __table_args__ = (
        sa.Index(None, "recipient_type", "recipient_id", "status"),
        sa.Index(None, "kind", "target", "recipient_id", "created_at"),
        sa.CheckConstraint("recipient_type IN ('professor', 'department')", name="recipient_type_valid"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'failed')", name="status_valid"),
    )

    id: uuid.UUID = id_field()
    kind: str = Field(max_length=40)  # propose_pod_stop, propose_key_disable, ...
    target: str = Field(max_length=200)  # pod id, key id, "team-b->team-a" or professor id
    reason: str = Field(sa_type=sa.Text)  # ISSUE / EVIDENCE / PROPOSED ACTION / IF IGNORED
    recipient_type: str = Field(max_length=20)
    recipient_id: uuid.UUID
    group_id: uuid.UUID | None = Field(default=None, foreign_key="groups.id")
    payload: dict[str, Any] = json_field()
    status: str = Field(default="pending", max_length=20, sa_column_kwargs={"server_default": "pending"})
    decided_by: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    decided_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    note: str = Field(default="", sa_type=sa.Text, sa_column_kwargs={"server_default": ""})
    result: dict[str, Any] | None = Field(default=None, sa_type=sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"))
    created_at: datetime = created_at_field()


class Notification(SQLModel, table=True):
    """A warning to the members of one group, written by the agent's warn_user tool."""

    __tablename__ = "notifications"
    __table_args__ = (sa.Index(None, "group_id", "created_at"),)

    id: uuid.UUID = id_field()
    group_id: uuid.UUID = Field(foreign_key="groups.id", ondelete="CASCADE")
    message: str = Field(sa_type=sa.Text)
    level: str = Field(default="warning", max_length=20)
    read: bool = Field(default=False, sa_column_kwargs={"server_default": sa.false()})
    created_at: datetime = created_at_field()
