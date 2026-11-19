"""get_budgets (Person 2, Week 3): one team's budget this period, from the real ledger.

Read-only. Same shape as evaluation/world/sim_server.py, so the agent sees the same thing in
the evaluation and on the real database:

    {"status": "ok", "team_id", "name", "balance_usd", "spent_usd", "remaining_usd",
     "pct_used", "period_day", "period_days", ...}

Two extra fields help the agent NOT repeat what the rules already did (design: the agent
does not repeat rule-based alerts):

    budget_status   "ok" under 80%, "warning" from 80%, "exhausted" from 100% (the watchdog
                    has already stopped pods and capped keys)
    rule_alerts     the 80% / 100% notices the watchdog already sent this period

The arithmetic is done here (Decimal, from the ledger), never by the model.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.budget.ledger import budget_status, period_of, team_totals
from app.models import AuditLog, Group, LedgerEntry

RULE_ALERTS = {"budget.warn": "80%", "budget.exhausted": "100%"}


def _team_id(team_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(team_id))
    except ValueError:
        return None


def _period_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def tool_get_budgets(session: Session, team_id: str, now: datetime | None = None) -> dict:
    """Agent tool: budget status of one team this period."""
    gid = _team_id(team_id)
    group = session.get(Group, gid) if gid else None
    if group is None:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    now = now or datetime.now(timezone.utc)
    period = period_of(now)

    entries = session.exec(
        select(LedgerEntry).where(LedgerEntry.group_id == gid, LedgerEntry.period == period)
    ).all()
    t = team_totals(entries, gid, period)
    balance, spent = float(t.funded), float(t.spent)

    alerts = session.exec(
        select(AuditLog).where(
            AuditLog.target_type == "group",
            AuditLog.target_id == str(gid),
            AuditLog.action.in_(list(RULE_ALERTS)),  # type: ignore[attr-defined]
            AuditLog.at >= _period_start(now),
        ).order_by(AuditLog.at)
    ).all()

    return {
        "status": "ok",
        "team_id": str(gid),
        "name": group.name,
        "period": period,
        "balance_usd": round(balance, 2),
        "spent_usd": round(spent, 2),
        "remaining_usd": round(max(balance - spent, 0.0), 2),
        # Same rule as the sim: a team with no money counts as fully used.
        "pct_used": round(spent / balance, 3) if balance > 0 else 1.0,
        "period_day": now.day,
        "period_days": calendar.monthrange(now.year, now.month)[1],
        "budget_status": budget_status(t).value,
        "rule_alerts": [
            {"threshold": RULE_ALERTS[a.action], "at": a.at.astimezone(timezone.utc).isoformat()}
            for a in alerts
        ],
    }