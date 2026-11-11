"""Month-end rollover. The POST /jobs/rollover endpoint (B7) calls plan_rollover() and
saves what it returns.

Rule (proposed in docs/decisions.md, "rollover cap"):
    carry = what was left unused this month, if rollover is on (never negative)
    the cap limits the TOTAL a team can start a month with:
        carry <= rollover_cap - monthly_amount
So FYP Team 1 ($50/month, cap $100) that spends nothing has $100 in month 2, and still
$100 (not $150) in month 3 if it again spends nothing. No cap means unlimited carry.

What carries:
    group      its remaining money
    user       his remaining sub-budget in that group
    professor  only money he never gave to a group (groups carry their own leftovers,
               so carrying it for the professor too would count it twice)
A tracking-only professor has nothing to carry. Overspend is not carried as debt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal

from app.budget.ledger import (
    ZERO,
    EntryLike,
    money,
    next_period,
    professor_totals,
    team_totals,
    user_totals,
)
from app.models import BudgetPolicy, LedgerEntry, LedgerType, ScopeType


def rollover_amount(
    remaining: Decimal, monthly_amount: Decimal | None, rollover: bool, cap: Decimal | None
) -> Decimal:
    """How much carries into next month for one budget."""
    if not rollover or monthly_amount is None:
        return ZERO
    carry = max(ZERO, money(remaining))
    if cap is not None:
        carry = min(carry, max(ZERO, money(cap) - money(monthly_amount)))
    return money(carry)


def plan_rollover(
    policies: Iterable[BudgetPolicy],
    entries: Iterable[EntryLike],
    period: str,
    groups_by_professor: Mapping[uuid.UUID, Iterable[uuid.UUID]],
) -> list[LedgerEntry]:
    """The rollover lines to write at the start of next month. The job that saves them
    must first check they don't already exist, so running it twice doesn't double them."""
    entries = list(entries)
    new_period = next_period(period)
    out: list[LedgerEntry] = []
    for p in policies:
        if p.scope_type == ScopeType.group:
            remaining = team_totals(entries, p.scope_id, period).remaining
            group_id = p.scope_id
        elif p.scope_type == ScopeType.user:
            assert p.group_id is not None
            remaining = user_totals(entries, p.scope_id, p.group_id, period).remaining
            group_id = p.group_id
        else:
            prof = professor_totals(entries, p.scope_id, groups_by_professor.get(p.scope_id, ()), period)
            remaining = prof.unallocated
            group_id = None
        carry = rollover_amount(remaining, p.monthly_amount, p.rollover, p.rollover_cap)
        if carry > ZERO:
            out.append(LedgerEntry(
                scope_type=p.scope_type, scope_id=p.scope_id, group_id=group_id, period=new_period,
                type=LedgerType.rollover, amount=carry, note=f"Unused budget from {period}",
            ))
    return out