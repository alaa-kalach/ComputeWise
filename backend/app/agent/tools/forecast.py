"""forecast_spend: project a team's spending to the end of the budget period.

Pure function: numbers in, numbers out. The agent calls this tool and explains
the result; it never does the arithmetic itself.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlmodel import Session

from ._data import daily_spend, month_entries, period_info


def forecast_spend(
    daily_spend: Sequence[float],
    days_in_period: int,
    balance_usd: float,
    window_days: int = 7,
) -> dict:
    """Project spend to the end of the period from the recent daily rate.

    daily_spend:    USD spent on each day so far, oldest first
                    (len = days elapsed in the period).
    days_in_period: total days in the budget period (e.g. 30).
    balance_usd:    money available this period (monthly allocation + rollover).
    window_days:    how many recent days make up the "current rate".
    """
    if days_in_period <= 0:
        raise ValueError("days_in_period must be positive")
    if balance_usd < 0:
        raise ValueError("balance_usd cannot be negative")
    if any(x < 0 for x in daily_spend):
        raise ValueError("daily_spend cannot contain negative values")

    days_elapsed = len(daily_spend)
    days_left = max(days_in_period - days_elapsed, 0)
    spent = round(sum(daily_spend), 2)

    recent = list(daily_spend)[-window_days:]
    daily_rate = round(sum(recent) / len(recent), 2) if recent else 0.0

    projected_total = round(spent + daily_rate * days_left, 2)
    will_exceed = projected_total > balance_usd

    days_until_runout: Optional[float] = None
    if spent >= balance_usd:
        days_until_runout = 0.0
    elif will_exceed and daily_rate > 0:
        days_until_runout = round((balance_usd - spent) / daily_rate, 1)

    return {
        "spent_usd": spent,
        "balance_usd": round(balance_usd, 2),
        "remaining_usd": round(max(balance_usd - spent, 0.0), 2),
        "pct_used": round(spent / balance_usd, 3) if balance_usd else 1.0,
        "days_elapsed": days_elapsed,
        "days_left": days_left,
        "daily_rate_usd": daily_rate,
        "projected_total_usd": projected_total,
        "will_exceed": will_exceed,
        "days_until_runout": days_until_runout,
    }


def tool_forecast_spend(session: Session, team_id: str, now: datetime | None = None) -> dict:
    """Agent tool: forecast one team's spend for this month from the real ledger."""
    try:
        gid = uuid.UUID(str(team_id))
    except ValueError:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    now = now or datetime.now(timezone.utc)
    balance, days_in_period = period_info(session, gid, now)
    series = daily_spend(month_entries(session, gid, now), lambda e: "team", now).get("team", [0.0] * now.day)
    if balance <= 0 and sum(series) == 0:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    return {"status": "ok", "team_id": str(gid), **forecast_spend(series, days_in_period, balance)}
