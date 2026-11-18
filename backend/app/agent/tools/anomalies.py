"""detect_spend_anomalies: flag a key or pod whose spend suddenly jumps
above its own recent baseline.

Pure function: numbers in, numbers out.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Mapping, Sequence

from sqlmodel import Session

from ._data import daily_spend, month_entries


def detect_spend_anomalies(
    daily_spend_by_resource: Mapping[str, Sequence[float]],
    baseline_days: int = 7,
    factor: float = 3.0,
    min_jump_usd: float = 1.0,
) -> list[dict]:
    """Return resources whose latest day is far above their recent baseline.

    A resource is flagged when its latest day's spend is at least `factor`
    times its baseline (the mean of the `baseline_days` days before it) AND
    exceeds the baseline by at least `min_jump_usd`. The second condition
    stops tiny amounts (a $0.02 key jumping to $0.10) from raising alarms.
    Resources with fewer than 3 days of history are skipped.
    """
    flagged = []
    for resource_id, series in daily_spend_by_resource.items():
        series = list(series)
        if len(series) < 3:
            continue
        latest = series[-1]
        history = series[:-1][-baseline_days:]
        baseline = sum(history) / len(history)
        jump = latest - baseline
        ratio = latest / baseline if baseline > 0 else float("inf")
        if jump >= min_jump_usd and ratio >= factor:
            flagged.append(
                {
                    "resource_id": resource_id,
                    "latest_usd": round(latest, 2),
                    "baseline_usd": round(baseline, 2),
                    "ratio": round(ratio, 1) if ratio != float("inf") else None,
                }
            )
    flagged.sort(key=lambda a: a["latest_usd"] - a["baseline_usd"], reverse=True)
    return flagged


def tool_detect_spend_anomalies(session: Session, team_id: str, now: datetime | None = None) -> dict:
    """Agent tool: flag keys or pods of one team whose latest day jumped, from the ledger."""
    try:
        gid = uuid.UUID(str(team_id))
    except ValueError:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    now = now or datetime.now(timezone.utc)
    entries = month_entries(session, gid, now)
    by_resource = daily_spend(entries, lambda e: str(e.resource_id), now)
    return {"status": "ok", "team_id": str(gid), "anomalies": detect_spend_anomalies(by_resource)}
