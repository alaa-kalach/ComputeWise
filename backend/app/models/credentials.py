"""Provider credentials, department grants, and the one-per-provider binding."""

import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import (
    CredentialStatus,
    GrantStatus,
    OwnerType,
    Provider,
    created_at_field,
    enum_type,
    id_field,
    money_field,
)


class ProviderCredential(SQLModel, table=True):
    """An OpenRouter management key or a RunPod API key, owned by the department or a
    professor. The secret is Fernet-encrypted (app.core.crypto) and never returned by any API."""

    __tablename__ = "provider_credentials"
    __table_args__ = (sa.Index(None, "owner_type", "owner_id"),)

    id: uuid.UUID = id_field()
    owner_type: OwnerType = Field(sa_type=enum_type(OwnerType))
    # departments.id or professors.id depending on owner_type (polymorphic, so no FK).
    owner_id: uuid.UUID
    provider: Provider = Field(sa_type=enum_type(Provider))
    secret_encrypted: str = Field(sa_type=sa.Text)
    label: str = Field(max_length=200)
    status: CredentialStatus = Field(
        default=CredentialStatus.active,
        sa_type=enum_type(CredentialStatus),
        sa_column_kwargs={"server_default": CredentialStatus.active.value},
    )
    created_by: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    created_at: datetime = created_at_field()


class CredentialGrant(SQLModel, table=True):
    """The department lets a professor use one of its credentials, optionally with a USD
    ceiling. This grant ceiling is separate from the professor's budget policy (A10)."""

    __tablename__ = "credential_grants"
    __table_args__ = (
        # At most one active grant of a credential to the same professor.
        sa.Index(
            "uq_credential_grants_active",
            "credential_id",
            "professor_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
    )

    id: uuid.UUID = id_field()
    credential_id: uuid.UUID = Field(foreign_key="provider_credentials.id", index=True)
    professor_id: uuid.UUID = Field(foreign_key="professors.id", index=True)
    ceiling_usd: Decimal | None = money_field(nullable=True, default=None)
    status: GrantStatus = Field(
        default=GrantStatus.active,
        sa_type=enum_type(GrantStatus),
        sa_column_kwargs={"server_default": GrantStatus.active.value},
    )
    created_at: datetime = created_at_field()


class ProfessorProviderBinding(SQLModel, table=True):
    """Which credential a professor uses for each provider: his own or a granted one.
    The primary key (professor_id, provider) enforces one active credential per provider."""

    __tablename__ = "professor_provider_binding"

    professor_id: uuid.UUID = Field(foreign_key="professors.id", primary_key=True, ondelete="CASCADE")
    provider: Provider = Field(sa_type=enum_type(Provider), primary_key=True)
    credential_id: uuid.UUID = Field(foreign_key="provider_credentials.id", index=True)
    created_at: datetime = created_at_field()
