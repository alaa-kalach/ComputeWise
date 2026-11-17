"""End-to-end test of loop + gates + simulated server with a scripted model.
No API key and no network needed."""
import json

from app.agent.agent import solve
from evaluation.world.scoring import score
from evaluation.world.sim_server import SimServer
from evaluation.world.world import SCENARIO_DIR, load_scenario

SCENARIO = SCENARIO_DIR / "keys_pods" / "keys_pods_01.json"


class ScriptedLLM:
    """Replies with a fixed list of steps. A step is a tool call or final text."""

    def __init__(self, steps):
        self.steps, self.i = steps, 0

    def chat(self, messages, tools=None, cache_tag=""):
        step = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if isinstance(step, str):
            return {"content": step, "tool_calls": [], "usage": usage}
        name, args = step
        return {"content": None, "usage": usage,
                "tool_calls": [{"id": f"c{self.i}", "name": name,
                                "arguments": json.dumps(args)}]}


REASON = ("ISSUE: pod-1 idle for 40 hours\nEVIDENCE: spend 24.48 in 3 days, team runs out in 3.7 days\n"
          "PROPOSED ACTION: stop pod-1\nIF IGNORED: budget exhausted in about 4 days")


def run(steps):
    scenario = load_scenario(SCENARIO)
    server = SimServer(scenario)
    results = solve(server, scenario, llm=ScriptedLLM(steps), cache_tag="t")
    return scenario, server, results


def test_good_agent_passes_the_scenario():
    scenario, server, _ = run([
        ("get_budgets", {"team_id": "team-alpha"}),
        ("forecast_spend", {"team_id": "team-alpha"}),
        ("list_idle_pods", {"team_id": "team-alpha"}),
        ("propose_pod_stop", {"pod_id": "pod-1", "reason": REASON}),
        "Proposed stopping the idle pod.",
    ])
    assert score(server.actions, scenario)["passed"]


def test_gate_sends_a_lazy_agent_back_to_look_at_the_data():
    scenario, server, results = run([
        "Everything looks fine.",                       # finishes without looking
        ("get_budgets", {"team_id": "team-alpha"}),
        ("forecast_spend", {"team_id": "team-alpha"}),
        "Still nothing to do.",
    ])
    kinds = [t["kind"] for t in results[0]["trace"]]
    assert "gate" in kinds
    assert not score(server.actions, scenario)["passed"]  # looked, but did not act


def test_incomplete_proposal_is_refused_and_not_recorded():
    scenario, server, _ = run([
        ("get_budgets", {"team_id": "team-alpha"}),
        ("forecast_spend", {"team_id": "team-alpha"}),
        ("propose_pod_stop", {"pod_id": "pod-1", "reason": "idle"}),
        "done",
    ])
    assert server.actions == []


def test_forbidden_tool_is_not_available_to_the_agent():
    scenario, server, results = run([
        ("get_budgets", {"team_id": "team-alpha"}),
        ("forecast_spend", {"team_id": "team-alpha"}),
        ("stop_pod", {"pod_id": "pod-1"}),
        "done",
    ])
    tool_steps = [t for t in results[0]["trace"] if t.get("tool") == "stop_pod"]
    assert tool_steps and tool_steps[0]["status"] == "failed"
    assert server.actions == []   # blocked before reaching the server


def test_department_agent_escalates_a_professor_heading_past_his_ceiling():
    scenario = load_scenario(SCENARIO_DIR / "forecast" / "forecast_06.json")
    server = SimServer(scenario)
    solve(server, scenario, cache_tag="t", llm=ScriptedLLM([
        ("get_professor_budget", {"professor_id": "prof-2"}),
        ("escalate_to_department", {"professor_id": "prof-2", "reason": REASON}),
        "Escalated.",
    ]))
    assert score(server.actions, scenario)["passed"]


def test_department_budget_tool_numbers():
    scenario = load_scenario(SCENARIO_DIR / "forecast" / "forecast_06.json")
    r = SimServer(scenario).get_professor_budget("prof-2")
    assert r["grant_ceiling_usd"] == 100.0
    assert 0.7 < r["pct_of_ceiling_spent"] < 0.85     # a threshold alert at 80% stays quiet
    assert r["will_exceed_ceiling"] is True
