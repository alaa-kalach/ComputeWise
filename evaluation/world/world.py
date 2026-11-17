"""Load scenarios and answer simple questions about the simulated world."""
from __future__ import annotations

import json
from pathlib import Path

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def load_scenario(path) -> dict:
    with open(path) as f:
        scenario = json.load(f)
    validate(scenario)
    return scenario


def load_all(area: str | None = None, split: str | None = None) -> list[dict]:
    """All scenarios, optionally only one area and/or one split ("tune" or "heldout")."""
    pattern = f"{area}/*.json" if area else "*/*.json"
    found = [load_scenario(p) for p in sorted(SCENARIO_DIR.glob(pattern))]
    if split:
        found = [s for s in found if s.get("split", "tune") == split]
    return found


def validate(scenario: dict) -> None:
    """Fail loudly on a malformed scenario, so mistakes show up early."""
    day = scenario["period"]["day"]
    team_ids = {t["id"] for t in scenario["teams"]}
    if scenario.get("role") == "department" and not scenario.get("professors"):
        raise ValueError(f"{scenario['id']}: a department scenario needs professors")
    for prof in scenario.get("professors", []):
        if prof.get("credential", "department") not in ("department", "own"):
            raise ValueError(f"{scenario['id']}: credential must be 'department' or 'own'")
    for r in scenario["resources"]:
        if r["team_id"] not in team_ids:
            raise ValueError(f"{scenario['id']}: {r['id']} has unknown team")
        if len(r["daily_spend"]) != day:
            raise ValueError(
                f"{scenario['id']}: {r['id']} has {len(r['daily_spend'])} "
                f"days of spend, expected {day}"
            )


def team_resources(scenario: dict, team_id: str) -> list[dict]:
    return [r for r in scenario["resources"] if r["team_id"] == team_id]


def team_daily_spend(scenario: dict, team_id: str) -> list[float]:
    """Total spend of a team on each day (all its keys and pods added up)."""
    day = scenario["period"]["day"]
    totals = [0.0] * day
    for r in team_resources(scenario, team_id):
        for i, usd in enumerate(r["daily_spend"]):
            totals[i] += usd
    return [round(x, 2) for x in totals]
