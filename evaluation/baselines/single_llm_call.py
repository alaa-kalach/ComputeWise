"""Baseline 2: ONE model call per scenario. No tools, no loop, no gates.

The model gets every fact the agent could have fetched (budgets, usage,
forecast, anomalies, idle pods, professor budgets), all at once in the prompt,
and answers with a JSON list of actions. This is generous to the baseline: it
never has to decide what to look at. What it lacks is the loop, the gates and
the checking of its own proposals.
"""
from __future__ import annotations

import json

from app.agent.llm import LLM
from evaluation.world.sim_server import SimServer

SYSTEM = """You are the ComputeWise budget analyst. A lab gives each team a monthly budget in USD for API keys and GPU pods. Below is everything known about the subject you review. Decide which actions, if any, are needed.
You can only warn or propose; a human approves every change.
Available actions (tool: target):
- warn_user: team id
- propose_pod_stop: pod id
- propose_key_disable: key id
- propose_budget_change: "from_team_id->to_team_id"
- escalate_to_department: professor id
Answer with JSON only, in this form: {"actions": [{"tool": "...", "target": "..."}]}
Use an empty list when no action is needed."""


def facts(scenario: dict) -> dict:
    server = SimServer(scenario)
    out = {"period": scenario["period"]}
    if scenario.get("role") == "department":
        out["professors"] = [server.get_professor_budget(p["id"])
                             for p in scenario["professors"]]
        out["teams"] = {t["id"]: {"forecast": server.forecast_spend(t["id"]),
                                  "anomalies": server.detect_spend_anomalies(t["id"])}
                        for t in scenario["teams"]}
        return out
    out["teams"] = {}
    for t in scenario["teams"]:
        tid = t["id"]
        out["teams"][tid] = {
            "budget": server.get_budgets(tid),
            "usage": server.get_usage(tid),
            "forecast": server.forecast_spend(tid),
            "anomalies": server.detect_spend_anomalies(tid),
            "idle_pods": server.list_idle_pods(tid),
        }
    return out


def parse_actions(text: str) -> list[dict]:
    """Pull the JSON object out of the reply; a bad reply means no actions."""
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return [{"tool": a["tool"], "target": a.get("target")} for a in data["actions"]]
    except (ValueError, KeyError, TypeError):
        return []


def run(scenario: dict, llm: LLM, cache_tag: str = "") -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(facts(scenario), indent=1)}]
    reply = llm.chat(messages, tools=None, cache_tag=cache_tag)
    return parse_actions(reply["content"] or "")
