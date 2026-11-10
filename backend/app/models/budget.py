"""Budget policies and the append-only ledger.

Remaining budget is the SUM of ledger entries, never a stored balance column.
Sign convention: allocation and rollover are positive, spend is negative,
adjustment is either (a top-up is positive, a correction can be negative).

ledger_entries is append-only: a database trigger (see the initial migration) rejects
UPDATE and DELETE. Fix a mistake by writing an adjustment entry.
"""

import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import (
    LedgerType,
    Provider,
    ScopeType,
    created_at_field,
    enum_type,
    id_field,
    money_field,
    utcnow,
)


class BudgetPolicy(SQLModel, table=True):
    """Monthly USD budget for a professor, a group, or a user within a group.

    Two additions to the design:
    - group_id: required when scope_type = 'user', so a student in groups under two
      professors has a separate policy under each.
    - monthly_amount NULL on a professor policy means "tracking only": the department
      observes but sets no ceiling (A10, open question 1).
    """

    __tablename__ = "budget_policies"
    __table_args__ = (
        sa.UniqueConstraint("scope_type", "scope_id", "group_id", postgresql_nulls_not_distinct=True),
        sa.CheckConstraint("scope_type <> 'user' OR group_id IS NOT NULL", name="user_scope_needs_group"),
        sa.CheckConstraint("monthly_amount IS NULL OR monthly_amount >= 0", name="amount_non_negative"),
        sa.CheckConstraint("rollover_cap IS NULL OR rollover_cap >= 0", name="cap_non_negative"),
    )

    id: uuid.UUID = id_field()
    scope_type: ScopeType = Field(sa_type=enum_type(ScopeType))
    # professors.id, groups.id or users.id depending on scope_type (polymorphic, so no FK).
    scope_id: uuid.UUID
    group_id: uuid.UUID | None = Field(default=None, foreign_key="groups.id", index=True)
    monthly_amount: Decimal | None = money_field(nullable=True, default=None)
    rollover: bool = Field(default=True, sa_column_kwargs={"server_default": sa.true()})
    rollover_cap: Decimal | None = money_field(nullable=True, default=None)
    created_at: datetime = created_at_field()
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"server_default": sa.func.now(), "onupdate": sa.func.now()},
        nullable=False,
    )


class LedgerEntry(SQLModel, table=True):
    """Every movement of money. APPEND-ONLY (enforced by a trigger).

    group_id (added to the design) is set on every entry that belongs to a group,
    including user-scope entries, so a user's money under two professors stays separate.
    Spend entries record the provider and the credential_id that paid (the funding source
    used for billable vs observed reports) and the issued resource that spent it.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        sa.Index(None, "scope_type", "scope_id", "period"),
        sa.Index(None, "group_id", "period"),
        sa.CheckConstraint(r"period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="period_format"),
        sa.CheckConstraint("scope_type <> 'user' OR group_id IS NOT NULL", name="user_scope_needs_group"),
        sa.CheckConstraint(
            "type <> 'spend' OR (provider IS NOT NULL AND credential_id IS NOT NULL)",
            name="spend_has_funding_source",
        ),
    )

    id: uuid.UUID = id_field()
    scope_type: ScopeType = Field(sa_type=enum_type(ScopeType))
    scope_id: uuid.UUID
    group_id: uuid.UUID | None = Field(default=None, foreign_key="groups.id")
    period: str = Field(sa_type=sa.CHAR(7))  # 'YYYY-MM'
    type: LedgerType = Field(sa_type=enum_type(LedgerType))
    amount: Decimal = money_field()
    provider: Provider | None = Field(default=None, sa_type=enum_type(Provider))
    credential_id: uuid.UUID | None = Field(default=None, foreign_key="provider_credentials.id", index=True)
    resource_id: uuid.UUID | None = Field(default=None, foreign_key="issued_resources.id", index=True)
    # Added to the design: the reason for an adjustment (top-up, correction).
    note: str | None = Field(default=None, sa_type=sa.Text)
    created_by: uuid.UUID | None = Field(default=None, foreign_key="users.id")  # NULL = system job
    created_at: datetime = created_at_field()
