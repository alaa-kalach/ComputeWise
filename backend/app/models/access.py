"""Access requests, issued keys and pods, and usage snapshots."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import (
    Provider,
    RequestStatus,
    ResourceStatus,
    created_at_field,
    enum_type,
    id_field,
    json_field,
    money_field,
    utcnow,
)


class AccessRequest(SQLModel, table=True):
    __tablename__ = "access_requests"
    __table_args__ = (sa.Index(None, "group_id", "status"),)

    id: uuid.UUID = id_field()
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    group_id: uuid.UUID = Field(foreign_key="groups.id")
    provider: Provider = Field(sa_type=enum_type(Provider))
    justification: str | None = Field(default=None, sa_type=sa.Text)
    status: RequestStatus = Field(
        default=RequestStatus.pending,
        sa_type=enum_type(RequestStatus),
        sa_column_kwargs={"server_default": RequestStatus.pending.value},
    )
    decided_by: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    decided_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    created_at: datetime = created_at_field()


class IssuedResource(SQLModel, table=True):
    """An OpenRouter key or a RunPod pod issued to a user in a group.

    external_ref: the OpenRouter key HASH (the key itself is shown once and never
    stored) or the RunPod pod id.
    """

    __tablename__ = "issued_resources"
    __table_args__ = (sa.UniqueConstraint("provider", "external_ref"),)

    id: uuid.UUID = id_field()
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    group_id: uuid.UUID = Field(foreign_key="groups.id", index=True)
    provider: Provider = Field(sa_type=enum_type(Provider))
    credential_id: uuid.UUID = Field(foreign_key="provider_credentials.id", index=True)
    external_ref: str = Field(max_length=255)
    current_limit: Decimal | None = money_field(nullable=True, default=None)  # OpenRouter key limit
    status: ResourceStatus = Field(sa_type=enum_type(ResourceStatus))
    # Added to the design: provider details such as gpu_type, cost_per_hr, disk_gb,
    # storage_cost_per_hr (pre-launch check, storage cost). Never secrets.
    details: dict[str, Any] = json_field()
    created_at: datetime = created_at_field()
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"server_default": sa.func.now(), "onupdate": sa.func.now()},
        nullable=False,
    )


class UsageSnapshot(SQLModel, table=True):
    """Cumulative spend of one resource at poll time. The sync job diffs consecutive
    snapshots and writes spend entries to the ledger."""

    __tablename__ = "usage_snapshots"
    __table_args__ = (sa.Index(None, "issued_resource_id", "polled_at"),)

    id: uuid.UUID = id_field()
    issued_resource_id: uuid.UUID = Field(foreign_key="issued_resources.id", ondelete="CASCADE")
    polled_at: datetime = created_at_field()
    cumulative_usd: Decimal = money_field()
