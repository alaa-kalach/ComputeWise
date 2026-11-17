"""Baseline 1: a rule script. Threshold alerts only, no reasoning.

Run from the repo root:
    python3 -m evaluation.baselines.rule_script
"""
from __future__ import annotations

from evaluation.world.scoring import score
from evaluation.world.world import load_all, team_daily_spend

WARN_AT = 0.8  # fraction of the balance spent


def run(scenario: dict) -> list[dict]:
    if scenario.get("role") == "department":
        return run_department(scenario)
    actions = []
    for team in scenario["teams"]:
        spent = sum(team_daily_spend(scenario, team["id"]))
        used = spent / team["balance_usd"] if team["balance_usd"] else 1.0
        if used >= WARN_AT:
            actions.append({"tool": "warn_user", "target": team["id"]})
    return actions


def run_department(scenario: dict) -> list[dict]:
    """Escalate a professor once his teams have spent 80% of his grant ceiling."""
    actions = []
    for prof in scenario["professors"]:
        spent = sum(sum(team_daily_spend(scenario, t["id"]))
                    for t in scenario["teams"] if t["professor_id"] == prof["id"])
        if spent / prof["grant_ceiling_usd"] >= WARN_AT:
            actions.append({"tool": "escalate_to_department", "target": prof["id"]})
    return actions


if __name__ == "__main__":
    results = [score(run(s), s) for s in load_all()]
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{status}  {r['scenario']}  missing={r['missing']}")
    passed = sum(r["passed"] for r in results)
    print(f"\nRule script: {passed}/{len(results)} passed")
