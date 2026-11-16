"""Budget endpoints (A4, B10). Every number is computed from the ledger (D8); no balance
is ever stored.

    GET /budget/summary          professor: his month at a glance
    GET /groups/{id}/budget      anyone who can see the group: team totals, and his own
                                 sub-budget if he has one (D9)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.budget.ledger import (
    ZERO,
    Totals,
    available_to_user,
    money,
    period_of,
    professor_totals,
    team_totals,
    user_totals,
)
from app.core.money import Money
from app.core.scoping import ScopeDep
from app.models import (
    BudgetPolicy,
    Group,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    OwnerType,
    ProfessorProviderBinding,
    Provider,
    ProviderCredential,
    ScopeType,
    UsageSnapshot,
)
from app.models.base import utcnow

router = APIRouter(tags=["budgets"])


# ====================================================================== API models
class TotalsOut(BaseModel):
    funded: Money
    spent: Money
    remaining: Money

    @classmethod
    def of(cls, t: Totals) -> "TotalsOut":
        return cls(funded=t.funded, spent=t.spent, remaining=t.remaining)


class PolicyOut(BaseModel):
    monthly_amount: Money
    rollover: bool
    rollover_cap: Money | None  # None = the default, 1 x monthly_amount (D2)


class ByProviderOut(BaseModel):
    openrouter: Money
    runpod: Money


class FundingOut(BaseModel):
    label: str
    billable: bool | None  # None = mixed: one provider paid by the department, the other not


class GroupBudgetOut(BaseModel):
    group_id: uuid.UUID
    period: str
    team: TotalsOut
    mine: TotalsOut | None
    available: Money
    policy: PolicyOut | None
    rolled_over: Money
    by_provider: ByProviderOut
    funding: FundingOut | None


class BudgetSummaryOut(BaseModel):
    period: str
    ceiling: Money | None
    allocated: Money
    unallocated: Money | None
    spent: Money
    last_synced_at: datetime | None


# ====================================================================== helpers
def current_period() -> str:
    return period_of(utcnow())


def _sum(entries: Iterable[LedgerEntry]) -> Decimal:
    return money(sum((e.amount for e in entries), ZERO))


def _entries(
    session: Session, period: str, group_ids: list[uuid.UUID], professor_id: uuid.UUID | None = None
) -> list[LedgerEntry]:
    """This month's ledger lines of these groups, plus the professor's own lines if given."""
    cond = LedgerEntry.group_id.in_(group_ids)  # type: ignore[union-attr]
    if professor_id is not None:
        cond = cond | ((LedgerEntry.scope_type == ScopeType.professor) & (LedgerEntry.scope_id == professor_id))
    return list(session.exec(select(LedgerEntry).where(LedgerEntry.period == period, cond)).all())


def _policy(
    session: Session, scope_type: ScopeType, scope_id: uuid.UUID, group_id: uuid.UUID | None = None
) -> BudgetPolicy | None:
    return session.exec(
        select(BudgetPolicy).where(
            BudgetPolicy.scope_type == scope_type,
            BudgetPolicy.scope_id == scope_id,
            BudgetPolicy.group_id == group_id,
        )
    ).first()


def _funding(session: Session, professor_id: uuid.UUID) -> FundingOut | None:
    """Which credentials pay for this professor's groups (his bindings, one per provider)."""
    creds = session.exec(
        select(ProviderCredential)
        .join(ProfessorProviderBinding, ProfessorProviderBinding.credential_id == ProviderCredential.id)
        .where(ProfessorProviderBinding.professor_id == professor_id)
        .order_by(ProviderCredential.provider)
    ).all()
    if not creds:
        return None
    kinds = {c.owner_type == OwnerType.department for c in creds}
    return FundingOut(label=" + ".join(c.label for c in creds), billable=kinds.pop() if len(kinds) == 1 else None)


def _last_synced(session: Session, group_ids: list[uuid.UUID]) -> datetime | None:
    if not group_ids:
        return None
    return session.exec(
        select(func.max(UsageSnapshot.polled_at))
        .join(IssuedResource, IssuedResource.id == UsageSnapshot.issued_resource_id)
        .where(IssuedResource.group_id.in_(group_ids))  # type: ignore[attr-defined]
    ).one()


def build_group_budget(session: Session, group: Group, viewer_id: uuid.UUID) -> GroupBudgetOut:
    """A group's budget as `viewer_id` sees it. The change endpoints answer with this too."""
    period = current_period()
    entries = _entries(session, period, [group.id])
    team = team_totals(entries, group.id, period)
    mine = None
    if _policy(session, ScopeType.user, viewer_id, group.id) is not None:
        mine = user_totals(entries, viewer_id, group.id, period)
    policy = _policy(session, ScopeType.group, group.id, group.id)
    spend = [e for e in entries if e.type == LedgerType.spend]
    return GroupBudgetOut(
        group_id=group.id,
        period=period,
        team=TotalsOut.of(team),
        mine=TotalsOut.of(mine) if mine else None,
        available=available_to_user(team, mine),
        policy=(
            PolicyOut(monthly_amount=policy.monthly_amount, rollover=policy.rollover, rollover_cap=policy.rollover_cap)
            if policy is not None and policy.monthly_amount is not None
            else None
        ),
        rolled_over=_sum(e for e in entries if e.scope_type == ScopeType.group and e.type == LedgerType.rollover),
        by_provider=ByProviderOut(
            openrouter=-_sum(e for e in spend if e.provider == Provider.openrouter),
            runpod=-_sum(e for e in spend if e.provider == Provider.runpod),
        ),
        funding=_funding(session, group.professor_id),
    )


# ====================================================================== endpoints
@router.get("/budget/summary", response_model=BudgetSummaryOut)
def budget_summary(scope: ScopeDep) -> BudgetSummaryOut:
    if not scope.me.is_professor or scope.me.professor_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only professors have a budget summary")
    session, period, prof_id = scope.session, current_period(), scope.me.professor_id
    group_ids = list(session.exec(select(Group.id).where(Group.professor_id == prof_id)).all())
    totals = professor_totals(_entries(session, period, group_ids, prof_id), prof_id, group_ids, period)
    policy = _policy(session, ScopeType.professor, prof_id)
    has_ceiling = policy is not None and policy.monthly_amount is not None  # else tracking only (D1)
    return BudgetSummaryOut(
        period=period,
        ceiling=totals.funded if has_ceiling else None,
        allocated=totals.allocated,
        unallocated=totals.unallocated if has_ceiling else None,
        spent=totals.spent,
        last_synced_at=_last_synced(session, group_ids),
    )


@router.get("/groups/{group_id}/budget", response_model=GroupBudgetOut)
def group_budget(group_id: uuid.UUID, scope: ScopeDep) -> GroupBudgetOut:
    group = scope.require_group(group_id)  # 404 if outside the caller's scope (D11)
    return build_group_budget(scope.session, group, scope.me.id)