"""Shared database reads for the forecast and anomaly tools (read-only).

Daily spend comes from the ledger: spend entries of one team in the current month.
Spend amounts are stored as negative numbers, so we flip the sign.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.budget.ledger import period_of, team_totals
from app.models import LedgerEntry, LedgerType


def month_entries(session: Session, team_id: uuid.UUID, now: datetime) -> list[LedgerEntry]:
    period = period_of(now)
    return list(
        session.exec(
            select(LedgerEntry).where(LedgerEntry.group_id == team_id, LedgerEntry.period == period)
        ).all()
    )


def daily_spend(entries: list[LedgerEntry], key, now: datetime) -> dict:
    """{key(entry): [USD per day, day 1 .. today]} for this month's spend entries."""
    days = now.day
    out: dict = {}
    for e in entries:
        if e.type != LedgerType.spend:
            continue
        series = out.setdefault(key(e), [0.0] * days)
        d = e.created_at.astimezone(timezone.utc).day
        if 1 <= d <= days:
            series[d - 1] += float(-e.amount)
    return out


def period_info(session: Session, team_id: uuid.UUID, now: datetime) -> tuple[float, int]:
    """(balance_usd, days_in_period) for the team this month."""
    totals = team_totals(month_entries(session, team_id, now), team_id, period_of(now))
    return float(totals.funded), calendar.monthrange(now.year, now.month)[1]
