"""Month-end rollover. The POST /jobs/rollover endpoint (B7) calls plan_rollover() and
saves what it returns.

Rule (docs/decisions.md, D2): the cap limits the AMOUNT CARRIED, not the total.
    carry = what was left unused this month, if rollover is on (never negative),
            but at most rollover_cap; with no cap, at most one month's budget
So FYP Team 1 ($50/month, no cap) that spends nothing has $100 in month 2, and still $100
(not $150) in month 3: a team never holds more than twice its monthly budget by default.

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
    """How much carries into next month for one budget (D2): what was left unused, at most
    the cap; with no cap, at most one month's budget. Never negative (overspend is no debt)."""
    if not rollover or monthly_amount is None:
        return ZERO
    limit = money(cap) if cap is not None else money(monthly_amount)
    return money(min(max(ZERO, money(remaining)), limit))


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