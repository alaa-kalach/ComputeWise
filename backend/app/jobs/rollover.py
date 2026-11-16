"""The new-month job (A4, B7): on the 1st, carry last month's unused money over and give
every budget its monthly amount.

    POST /jobs/rollover    pg_cron on the 1st of each month, just after midnight

For the month that just started:
    1. rollover lines: last month's leftovers, each capped as decision D2 says
       (plan_rollover in app/budget/rollover.py does the math)
    2. allocation lines: each saved budget's monthly amount (professor ceilings, team
       budgets, student limits). Tracking-only professors have none.

Safe to run twice: a budget that already has its rollover or its allocation this month is
skipped, so nothing is ever doubled. Professors removed by the department are skipped.
The ledger is append-only (D8): this job only adds lines.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session, select

from app.budget.ledger import ZERO, period_of, prev_period
from app.budget.rollover import plan_rollover
from app.core.audit import write_audit
from app.models import BudgetPolicy, Group, LedgerEntry, LedgerType, Professor, ScopeType
from app.models.base import utcnow

ROLLOVER_LOCK_ID = 7301002


@dataclass
class NewMonthResult:
    period: str
    rollover_entries: int = 0
    allocation_entries: int = 0
    skipped: bool = False  # another run held the lock


def _key(scope_type: ScopeType, scope_id: uuid.UUID, group_id: uuid.UUID | None) -> tuple:
    return (scope_type, scope_id, group_id)


def run_new_month(session: Session, now: datetime | None = None) -> NewMonthResult:
    """Start the month containing `now` (default: today). Commits its own work."""
    period = period_of(now or utcnow())
    result = NewMonthResult(period=period)
    if not session.execute(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": ROLLOVER_LOCK_ID}).scalar():
        result.skipped = True
        return result

    # Who still counts: groups of professors the department hasn't removed.
    groups = session.exec(
        select(Group).join(Professor, Professor.id == Group.professor_id).where(Professor.removed_at.is_(None))  # type: ignore[union-attr]
    ).all()
    live_profs = set(session.exec(select(Professor.id).where(Professor.removed_at.is_(None))).all())  # type: ignore[union-attr]
    live_groups = {g.id for g in groups}
    groups_by_prof: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for g in groups:
        groups_by_prof[g.professor_id].append(g.id)

    policies = [
        p for p in session.exec(select(BudgetPolicy)).all()
        if (p.scope_type == ScopeType.professor and p.scope_id in live_profs)
        or (p.scope_type != ScopeType.professor and p.group_id in live_groups)
    ]

    # Lines already written this month, so a second run adds nothing.
    this_month = session.exec(select(LedgerEntry).where(LedgerEntry.period == period)).all()
    has_rollover = {_key(e.scope_type, e.scope_id, e.group_id) for e in this_month if e.type == LedgerType.rollover}
    has_allocation = {_key(e.scope_type, e.scope_id, e.group_id) for e in this_month if e.type == LedgerType.allocation}

    # 1. Rollover from last month.
    last_month = session.exec(select(LedgerEntry).where(LedgerEntry.period == prev_period(period))).all()
    for line in plan_rollover(policies, last_month, prev_period(period), groups_by_prof):
        if _key(line.scope_type, line.scope_id, line.group_id) not in has_rollover:
            session.add(line)
            result.rollover_entries += 1

    # 2. This month's allocations.
    for p in policies:
        if p.monthly_amount is None or p.monthly_amount <= ZERO:
            continue  # tracking only, or a zero budget
        group_id = None if p.scope_type == ScopeType.professor else p.group_id
        if _key(p.scope_type, p.scope_id, group_id) in has_allocation:
            continue
        session.add(LedgerEntry(
            scope_type=p.scope_type, scope_id=p.scope_id, group_id=group_id, period=period,
            type=LedgerType.allocation, amount=p.monthly_amount, note="Monthly allocation",
        ))
        result.allocation_entries += 1

    session.flush()
    write_audit(session, None, "budget.new_month", "period", period, {
        "rollover_entries": result.rollover_entries, "allocation_entries": result.allocation_entries,
    })
    session.commit()
    return result