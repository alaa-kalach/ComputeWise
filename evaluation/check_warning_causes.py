"""Check that each warning says WHAT DROVE the spend (work split, Week 3).

For every tune scenario where the agent sends a warn_user, this compares the warning text with
the real top spend drivers of that team (from get_usage: the resources that spent the most in
the last 7 days). A warning passes if it names at least one of the top 3 drivers.

From the repo root, with OPENROUTER_API_KEY set:
    python3 -m evaluation.check_warning_causes
Only the tune split is used. Replies are cached, so a re-run costs nothing.
"""
from __future__ import annotations

import re

from app.agent.agent import solve
from evaluation.world.sim_server import SimServer
from evaluation.world.world import load_all


def mentions(message: str, resource_id: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(resource_id)}(?![\w-])", message, re.IGNORECASE) is not None


def main(cache_tag: str = "r1") -> int:
    checked = named = 0
    for scenario in load_all(split="tune"):
        if scenario.get("role", "team") != "team":
            continue
        server = SimServer(scenario)
        solve(server, scenario, cache_tag=cache_tag)
        for run in server.agent_results:
            warnings = [t["args"].get("message", "") for t in run["trace"]
                        if t["kind"] == "tool" and t["tool"] == "warn_user" and t.get("status") == "ok"]
            if not warnings:
                continue
            team_id = next(t["args"].get("team_id") for t in run["trace"]
                           if t["kind"] == "tool" and t["tool"] == "warn_user")
            drivers = [d["id"] for d in server.get_usage(team_id)["top_drivers"]]
            for msg in warnings:
                checked += 1
                ok = any(mentions(msg, d) for d in drivers)
                named += ok
                print(f"\n[{'OK ' if ok else 'MISS'}] {scenario['id']} / {team_id}   top drivers: {', '.join(drivers)}")
                print(f"       {msg}")
    print(f"\n{named} of {checked} warnings name a top spend driver.")
    return 0 if named == checked else 1


if __name__ == "__main__":
    raise SystemExit(main())
