"""Function-calling schemas for the budget-analyst agent's tools.

This file only DESCRIBES the tools to the model. The tools themselves are
implemented by a "server" object with a `call(name, **args)` method: in
evaluation that is `evaluation.world.sim_server.SimServer`, in production it
will call the real database and adapters. tests/insight/test_tool_schemas.py
checks that schemas and the simulated server stay in sync.

Never given to the agent (it can only PROPOSE, a human decides):
stop_pod, disable_key, raise_budget.
"""
from __future__ import annotations

READ_TOOLS = ["get_budgets", "get_usage", "forecast_spend",
              "detect_spend_anomalies", "list_idle_pods", "get_professor_budget"]
ACTION_TOOLS = ["warn_user", "propose_pod_stop", "propose_key_disable",
                "propose_budget_change", "escalate_to_department"]
FORBIDDEN_TOOLS = ("stop_pod", "disable_key", "raise_budget")

# Which tools each kind of run may use. A team run reviews one team for a
# professor; a department run reviews one professor for the department.
ROLE_TOOLS = {
    "team": ["get_budgets", "get_usage", "forecast_spend", "detect_spend_anomalies",
             "list_idle_pods", "warn_user", "propose_pod_stop", "propose_key_disable",
             "propose_budget_change"],
    "department": ["get_professor_budget", "get_usage", "forecast_spend",
                   "detect_spend_anomalies", "escalate_to_department"],
}


def _fn(name, description, properties=None, required=None):
    props = properties or {}
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required if required is not None else list(props),
            "additionalProperties": False,
        },
    }


_TEAM = {"type": "string", "description": "Id of the team, for example team-alpha."}
_REASON = {"type": "string",
           "description": "Four lines: ISSUE / EVIDENCE / PROPOSED ACTION / IF IGNORED."}

_FUNCTIONS = [
    _fn("get_budgets",
        "Budget status of one team this period: balance, spent, remaining, percent used, "
        "and how far into the period we are.", {"team_id": _TEAM}),
    _fn("get_usage",
        "Spend of one team broken down per key and per pod, with the last 7 days and the "
        "biggest spend drivers. Use it to explain WHY spend is high.", {"team_id": _TEAM}),
    _fn("forecast_spend",
        "Projected spend of one team at the end of the period, based on its recent daily "
        "rate, and how many days until the money runs out. The arithmetic is done for you.",
        {"team_id": _TEAM}),
    _fn("detect_spend_anomalies",
        "Keys or pods of one team whose latest day of spend is far above their own recent "
        "baseline.", {"team_id": _TEAM}),
    _fn("list_idle_pods",
        "Running pods of one team whose GPU has sat unused for a long time.",
        {"team_id": _TEAM}),
    _fn("get_professor_budget",
        "Department view of one professor: grant ceiling, money allocated to his teams, spent "
        "so far, projected demand to the end of the period, and the ids of his teams.",
        {"professor_id": {"type": "string", "description": "Id of the professor."}}),
    _fn("warn_user",
        "Send a warning to the members of a team. A warning changes nothing; use it when the "
        "team should slow down on its own.",
        {"team_id": _TEAM, "message": {"type": "string", "description": "The warning text. It must name the key or pod ids that drove most of the spend (the top drivers from get_usage) and what they spent, then the projected run-out."}}),
    _fn("propose_pod_stop",
        "Ask the professor to stop a pod. A human approves; nothing is stopped by this call.",
        {"pod_id": {"type": "string", "description": "Id of the pod."}, "reason": _REASON}),
    _fn("propose_key_disable",
        "Ask the professor to disable an API key. A human approves; nothing is disabled by "
        "this call.",
        {"key_id": {"type": "string", "description": "Id of the key."}, "reason": _REASON}),
    _fn("propose_budget_change",
        "Ask the professor to move budget from one team to another. A human approves.",
        {"from_team_id": _TEAM,
         "to_team_id": {"type": "string", "description": "Team that receives the money."},
         "amount_usd": {"type": "number", "description": "Amount in USD."},
         "reason": _REASON}),
    _fn("escalate_to_department",
        "Raise an issue with the department admin: a professor trending past his grant "
        "ceiling, or unusual spend on a department credential. A human decides.",
        {"professor_id": {"type": "string", "description": "Id of the professor."},
         "reason": _REASON}),
]

SCHEMAS = {f["name"]: f for f in _FUNCTIONS}


def tools_for(role: str) -> list[dict]:
    """Tools in Chat Completions format for a role ("team" or "department")."""
    return [{"type": "function", "function": SCHEMAS[n]} for n in ROLE_TOOLS[role]]
