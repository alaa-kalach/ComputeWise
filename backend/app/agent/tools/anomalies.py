"""detect_spend_anomalies: flag a key or pod whose spend suddenly jumps
above its own recent baseline.

Pure function: numbers in, numbers out.
"""
from __future__ import annotations

from typing import Mapping, Sequence


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
