"""Ledger arithmetic: pure functions over ledger entries, no database.

Remaining budget is never stored; it is the sum of entries for one period ('YYYY-MM').
Rollover moves unused money into the next period as one entry, so every period stands
on its own. Signs: allocation and rollover are positive, spend is negative,
adjustment is either.

Whose money is whose:
    team (group)   funded by group-scope allocation/rollover/adjustment entries, and
                   spent by every spend entry carrying that group_id.
    user           optional sub-budget inside one group. A user's spend counts against both
                   his own budget and his team's, so he can spend min(user left, team left).
    professor      funded by professor-scope entries. He hands money out to his groups;
                   'unallocated' is what he hasn't handed out yet.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.models import LedgerEntry, LedgerType, Provider, ScopeType

ZERO = Decimal("0")
CENT4 = Decimal("0.0001")
WARN_AT = Decimal("0.80")
CAP_AT = Decimal("1.00")
FUNDING = (LedgerType.allocation, LedgerType.rollover, LedgerType.adjustment)


def money(x: Decimal | int | str) -> Decimal:
    """Exact USD with 4 decimals. Never pass floats."""
    if isinstance(x, float):
        raise TypeError("money must be Decimal, int or str, never float")
    return Decimal(x).quantize(CENT4, rounding=ROUND_HALF_UP)


class EntryLike(Protocol):
    scope_type: ScopeType
    scope_id: uuid.UUID
    group_id: uuid.UUID | None
    period: str
    type: LedgerType
    amount: Decimal


def period_of(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def next_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def prev_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"


@dataclass(frozen=True)
class Totals:
    funded: Decimal
    spent: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.funded - self.spent

    @property
    def used_fraction(self) -> Decimal:
        """spent / funded; 1 or more means exhausted. A team with no money is exhausted."""
        if self.funded <= ZERO:
            return Decimal("1") if self.spent >= ZERO else ZERO
        return self.spent / self.funded


def _sum(entries: Iterable[EntryLike]) -> Decimal:
    return money(sum((e.amount for e in entries), ZERO))


def team_totals(entries: Iterable[EntryLike], group_id: uuid.UUID, period: str) -> Totals:
    rows = [e for e in entries if e.period == period and e.group_id == group_id]
    funded = _sum(e for e in rows if e.scope_type == ScopeType.group and e.type in FUNDING)
    spent = -_sum(e for e in rows if e.type == LedgerType.spend)
    return Totals(funded, spent)


def user_totals(entries: Iterable[EntryLike], user_id: uuid.UUID, group_id: uuid.UUID, period: str) -> Totals:
    rows = [
        e for e in entries
        if e.period == period and e.group_id == group_id
        and e.scope_type == ScopeType.user and e.scope_id == user_id
    ]
    funded = _sum(e for e in rows if e.type in FUNDING)
    spent = -_sum(e for e in rows if e.type == LedgerType.spend)
    return Totals(funded, spent)


@dataclass(frozen=True)
class ProfessorTotals(Totals):
    allocated: Decimal = ZERO

    @property
    def unallocated(self) -> Decimal:
        return self.funded - self.allocated


def professor_totals(
    entries: Iterable[EntryLike], professor_id: uuid.UUID, group_ids: Iterable[uuid.UUID], period: str
) -> ProfessorTotals:
    groups = set(group_ids)
    rows = [e for e in entries if e.period == period]
    funded = _sum(
        e for e in rows
        if e.scope_type == ScopeType.professor and e.scope_id == professor_id and e.type in FUNDING
    )
    allocated = _sum(
        e for e in rows
        if e.scope_type == ScopeType.group and e.group_id in groups and e.type == LedgerType.allocation
    )
    spent = -_sum(e for e in rows if e.group_id in groups and e.type == LedgerType.spend)
    return ProfessorTotals(funded, spent, allocated)


def available_to_user(team: Totals, user: Totals | None) -> Decimal:
    """What one user may still spend: the team's remaining, further limited by his own
    sub-budget if he has one. Never negative."""
    left = team.remaining if user is None else min(team.remaining, user.remaining)
    return max(ZERO, left)


class BudgetStatus(str, enum.Enum):
    ok = "ok"
    warning = "warning"
    exhausted = "exhausted"


def budget_status(t: Totals) -> BudgetStatus:
    """Under 80% ok, from 80% warning, from 100% exhausted (stop pods, cap keys)."""
    f = t.used_fraction
    if f >= CAP_AT:
        return BudgetStatus.exhausted
    if f >= WARN_AT:
        return BudgetStatus.warning
    return BudgetStatus.ok


def key_limit(key_usage: Decimal, available: Decimal) -> Decimal:
    """The design's formula: key limit = key usage so far + what the team has left.
    At 0 left, the limit equals the usage, which caps the key."""
    return money(key_usage + max(ZERO, available))


def usage_delta(previous_cumulative: Decimal | None, current_cumulative: Decimal) -> Decimal:
    """New spend between two total readings. A first reading counts from 0. A drop is
    never negative spend: it returns 0."""
    prev = ZERO if previous_cumulative is None else previous_cumulative
    return money(max(ZERO, current_cumulative - prev))


def allocation_entry(
    scope_type: ScopeType, scope_id: uuid.UUID, period: str, amount: Decimal, *,
    group_id: uuid.UUID | None = None, created_by: uuid.UUID | None = None, note: str | None = None,
) -> LedgerEntry:
    """A new, unsaved allocation line. The caller adds it to the database."""
    amount = money(amount)
    if amount < ZERO:
        raise ValueError("an allocation is never negative; reduce a budget with an adjustment")
    if scope_type != ScopeType.professor and group_id is None:
        raise ValueError("group and user allocations need group_id")
    return LedgerEntry(scope_type=scope_type, scope_id=scope_id, group_id=group_id, period=period,
                       type=LedgerType.allocation, amount=amount, created_by=created_by, note=note)


def spend_entry(
    user_id: uuid.UUID, group_id: uuid.UUID, period: str, amount: Decimal, *,
    provider: Provider, credential_id: uuid.UUID, resource_id: uuid.UUID,
) -> LedgerEntry:
    """A new, unsaved spend line, with the credential that paid (for billing reports).
    `amount` is the positive cost; it is stored as negative."""
    amount = money(amount)
    if amount <= ZERO:
        raise ValueError("spend amount must be positive (it is stored as negative)")
    return LedgerEntry(scope_type=ScopeType.user, scope_id=user_id, group_id=group_id, period=period,
                       type=LedgerType.spend, amount=-amount, provider=provider,
                       credential_id=credential_id, resource_id=resource_id)


def adjustment_entry(
    scope_type: ScopeType, scope_id: uuid.UUID, period: str, amount: Decimal, *, note: str,
    group_id: uuid.UUID | None = None, created_by: uuid.UUID | None = None,
) -> LedgerEntry:
    """A new, unsaved adjustment line (top-up or correction). It always needs a reason."""
    amount = money(amount)
    if amount == ZERO:
        raise ValueError("an adjustment of 0 changes nothing")
    if not note or not note.strip():
        raise ValueError("every adjustment needs a note saying why")
    if scope_type != ScopeType.professor and group_id is None:
        raise ValueError("group and user adjustments need group_id")
    return LedgerEntry(scope_type=scope_type, scope_id=scope_id, group_id=group_id, period=period,
                       type=LedgerType.adjustment, amount=amount, created_by=created_by, note=note.strip())