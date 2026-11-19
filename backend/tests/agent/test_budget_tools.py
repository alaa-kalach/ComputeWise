"""Person 2's agent tools on the real schema: get_budgets, warn_user and the propose_* tools.

Seed (now = Nov 10, 12:00): FYP Team 1 has $100 (50 allocated + 50 rolled over) and spent
$20.80 on Omar's running pod and Sara's key; FYP Team 2 has $50 and spent $41 (82%), its
only pod (Lina's) is stopped. Both teams belong to Prof A; Research Lab belongs to Prof B.
"""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import select

import app.adapters as adapters_pkg
from app.access.pods import available_budget
from app.adapters.mock import WORLD
from app.agent.tools.budgets import tool_get_budgets
from app.agent.tools.propose import (
    tool_propose_budget_change,
    tool_propose_key_disable,
    tool_propose_pod_stop,
)
from app.agent.tools.warn import tool_warn_user
from app.agent.tools_schema import ROLE_TOOLS
from app.core.audit import write_audit
from app.jobs import agent as job
from app.main import app
from app.models import AuditLog, Escalation, Notification
from tests.conftest import auth, seed_id

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
FYP1, FYP2, LAB = str(seed_id("fyp1")), str(seed_id("fyp2")), str(seed_id("research_lab"))
PROF_A = seed_id("prof_a")
OMAR_POD, LINA_POD = str(seed_id("res_omar_pod")), str(seed_id("res_lina_pod"))
SARA_KEY = str(seed_id("res_sara_fyp1_key"))
REASON = ("ISSUE: spend is high\nEVIDENCE: the tools say so\n"
          "PROPOSED ACTION: do the thing\nIF IGNORED: the team runs out")


def escalations(session, kind=None):
    q = select(Escalation)
    if kind:
        q = q.where(Escalation.kind == kind)
    return list(session.exec(q).all())


def audits(session, action):
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def team_notes(session, team):
    return list(session.exec(select(Notification).where(Notification.group_id == uuid.UUID(team))).all())


# ------------------------------------------------------------------ get_budgets
def test_budgets_add_up_from_the_ledger(session):
    out = tool_get_budgets(session, FYP1, now=NOW)
    assert out["status"] == "ok" and out["name"] == "FYP Team 1"
    assert out["balance_usd"] == 100.0 and out["spent_usd"] == 20.8 and out["remaining_usd"] == 79.2
    assert out["pct_used"] == 0.208 and out["budget_status"] == "ok"
    assert out["period_day"] == 10 and out["period_days"] == 30 and out["period"] == "2026-11"


def test_budgets_say_when_the_80_percent_rule_applies(session):
    out = tool_get_budgets(session, FYP2, now=NOW)
    assert out["pct_used"] == 0.82 and out["budget_status"] == "warning"


def test_budgets_show_the_alerts_the_watchdog_already_sent(session):
    """So the agent does not repeat a rule-based alert."""
    now = datetime.now(timezone.utc)
    assert tool_get_budgets(session, FYP2, now=now)["rule_alerts"] == []
    write_audit(session, None, "budget.warn", "group", seed_id("fyp2"), {"percent": 82})
    session.flush()
    alerts = tool_get_budgets(session, FYP2, now=now)["rule_alerts"]
    assert [a["threshold"] for a in alerts] == ["80%"]
    # another team's alert is not mixed in
    assert tool_get_budgets(session, FYP1, now=now)["rule_alerts"] == []


def test_budgets_unknown_team(session):
    assert tool_get_budgets(session, "team-nope")["status"] == "failed"
    assert tool_get_budgets(session, str(seed_id("nothing")))["status"] == "failed"


def test_budgets_match_the_simulated_servers_shape(session):
    """The agent must see the same fields in the evaluation and on the real database."""
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[3])  # the repo root, where evaluation/ lives
    if root not in sys.path:
        sys.path.insert(0, root)
    from evaluation.world.sim_server import SimServer
    sim = SimServer({"period": {"day": 1, "days": 30},
                     "teams": [{"id": "t", "name": "T", "professor_id": "p", "balance_usd": 10}],
                     "resources": []}).get_budgets("t")
    assert set(sim) <= set(tool_get_budgets(session, FYP1, now=NOW))


# ------------------------------------------------------------------ warn_user
def test_warning_is_saved_for_the_team_and_audited(session):
    out = tool_warn_user(session, FYP1, f"Most spend is pod {OMAR_POD}: $12.30.")
    assert out["status"] == "ok" and out["sent"] is True
    notes = team_notes(session, FYP1)
    assert len(notes) == 1 and OMAR_POD in notes[0].message and notes[0].level == "warning"
    assert [a.target_id for a in audits(session, "notification.warn")] == [FYP1]


def test_a_team_is_warned_once_per_cooldown(session):
    assert tool_warn_user(session, FYP1, "first")["sent"] is True
    again = tool_warn_user(session, FYP1, "second")
    assert again["status"] == "ok" and again["sent"] is False  # "ok", so the agent does not retry
    assert len(team_notes(session, FYP1)) == 1
    # another team is not affected
    assert tool_warn_user(session, FYP2, "other team")["sent"] is True


def test_the_agent_does_not_repeat_a_watchdog_notice(session):
    session.add(Notification(group_id=seed_id("fyp2"), message="FYP Team 2 has used 82%...", level="warning"))
    session.commit()
    out = tool_warn_user(session, FYP2, "You are spending fast.")
    assert out["sent"] is False and len(team_notes(session, FYP2)) == 1


def test_warning_needs_a_team_and_a_message(session):
    assert tool_warn_user(session, "nope", "hi")["status"] == "failed"
    assert tool_warn_user(session, FYP1, "   ")["status"] == "failed"
    assert team_notes(session, FYP1) == []


# ------------------------------------------------------------------ propose_pod_stop
def test_pod_stop_proposal_goes_to_the_teams_professor(session):
    out = tool_propose_pod_stop(session, OMAR_POD, REASON)
    assert out["status"] == "ok" and out["created"] is True
    [e] = escalations(session, "propose_pod_stop")
    assert e.target == OMAR_POD and e.status == "pending" and e.reason == REASON
    assert e.recipient_type == "professor" and e.recipient_id == PROF_A
    assert str(e.group_id) == FYP1 and str(e.id) == out["escalation_id"]
    assert [a.target_id for a in audits(session, "escalation.create")] == [str(e.id)]


def test_the_same_proposal_is_not_made_twice(session):
    first = tool_propose_pod_stop(session, OMAR_POD, REASON)
    again = tool_propose_pod_stop(session, OMAR_POD, REASON)
    assert again["status"] == "ok" and again["created"] is False
    assert again["escalation_id"] == first["escalation_id"]
    assert len(escalations(session, "propose_pod_stop")) == 1


def test_pod_stop_refuses_what_cannot_be_stopped(session):
    assert tool_propose_pod_stop(session, LINA_POD, REASON)["status"] == "failed"   # already stopped
    assert tool_propose_pod_stop(session, SARA_KEY, REASON)["status"] == "failed"   # a key, not a pod
    assert tool_propose_pod_stop(session, "pod-1", REASON)["status"] == "failed"
    assert escalations(session) == []


# ------------------------------------------------------------------ propose_key_disable
def test_key_disable_proposal(session):
    out = tool_propose_key_disable(session, SARA_KEY, REASON)
    assert out["status"] == "ok" and out["created"] is True
    [e] = escalations(session, "propose_key_disable")
    assert e.target == SARA_KEY and e.recipient_id == PROF_A and str(e.group_id) == FYP1


def test_key_disable_refuses_a_pod_or_an_unknown_id(session):
    assert tool_propose_key_disable(session, OMAR_POD, REASON)["status"] == "failed"
    assert tool_propose_key_disable(session, "key-1", REASON)["status"] == "failed"
    assert escalations(session) == []


# ------------------------------------------------------------------ propose_budget_change
def test_budget_move_proposal_carries_what_the_executor_needs(session):
    out = tool_propose_budget_change(session, FYP1, FYP2, 10, REASON, now=NOW)
    assert out["status"] == "ok" and out["created"] is True
    [e] = escalations(session, "propose_budget_change")
    assert e.target == f"{FYP1}->{FYP2}" and e.recipient_id == PROF_A and str(e.group_id) == FYP1
    assert e.payload == {"from_team_id": FYP1, "to_team_id": FYP2, "amount_usd": "10.0000"}


@pytest.mark.parametrize("args, why", [
    ((FYP1, FYP1, 5), "same team"),
    ((FYP1, LAB, 5), "different professors"),
    ((FYP1, FYP2, 0), "zero"),
    ((FYP1, FYP2, -3), "negative"),
    ((FYP1, FYP2, "lots"), "not a number"),
    ((FYP1, FYP2, float("nan")), "nan"),
    ((FYP1, FYP2, True), "a boolean"),
    ((FYP2, FYP1, 9.01), "more than FYP Team 2 has left ($9)"),
    (("nope", FYP2, 5), "unknown team"),
])
def test_budget_move_refuses_what_could_never_be_approved(session, args, why):
    out = tool_propose_budget_change(session, *args, REASON, now=NOW)
    assert out["status"] == "failed", why
    assert escalations(session) == []


def test_budget_move_of_exactly_what_is_left_is_allowed(session):
    assert tool_propose_budget_change(session, FYP2, FYP1, "9", REASON, now=NOW)["created"] is True


# ------------------------------------------------------------------ the four-line reason
@pytest.mark.parametrize("call", [
    lambda s, r: tool_propose_pod_stop(s, OMAR_POD, r),
    lambda s, r: tool_propose_key_disable(s, SARA_KEY, r),
    lambda s, r: tool_propose_budget_change(s, FYP1, FYP2, 5, r, now=NOW),
])
@pytest.mark.parametrize("reason", ["", "the pod is idle", "ISSUE: x\nEVIDENCE: y\nPROPOSED ACTION: z"])
def test_a_proposal_without_the_four_lines_is_refused(session, call, reason):
    out = call(session, reason)
    assert out["status"] == "failed" and "IF IGNORED" in out["detail"]
    assert escalations(session) == []


# ------------------------------------------------------------------ end to end
@pytest.fixture
def mock_providers(monkeypatch):
    monkeypatch.setattr(adapters_pkg, "adapter_mode", lambda: "mock")
    WORLD.reset()
    yield
    WORLD.reset()


def test_professor_approves_the_agents_budget_move_and_the_money_moves(client, session, mock_providers):
    a0, b0 = available_budget(session, seed_id("fyp1"), None), available_budget(session, seed_id("fyp2"), None)
    out = tool_propose_budget_change(session, FYP1, FYP2, 5, REASON)
    r = client.post(f"/escalations/{out['escalation_id']}/approve", headers=auth("karim"))
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert available_budget(session, seed_id("fyp1"), None) == a0 - Decimal(5)
    assert available_budget(session, seed_id("fyp2"), None) == b0 + Decimal(5)


def test_another_professor_cannot_approve_the_agents_proposal(client, session):
    out = tool_propose_key_disable(session, SARA_KEY, REASON)
    r = client.post(f"/escalations/{out['escalation_id']}/approve", headers=auth("rania"))
    assert r.status_code == 403


# ------------------------------------------------------------------ the agent job with the real tools
def test_every_team_tool_now_exists():
    """POST /jobs/agent answered 503 until these were written."""
    assert set(ROLE_TOOLS["team"]) <= set(job.discover_tools())


class ScriptedLLM:
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
                "tool_calls": [{"id": f"c{self.i}", "name": name, "arguments": json.dumps(args)}]}


def test_a_team_run_on_the_real_tools_warns_the_team(client, session, monkeypatch):
    monkeypatch.setenv("JOB_SECRET", "s")
    llm = ScriptedLLM([
        ("get_budgets", {"team_id": FYP1}),
        ("get_usage", {"team_id": FYP1}),
        ("forecast_spend", {"team_id": FYP1}),
        ("warn_user", {"team_id": FYP1, "message": f"Pod {OMAR_POD} drove most of the spend."}),
        "Warned the team.",
    ])
    app.dependency_overrides[job.get_llm] = lambda: llm
    try:
        r = client.post("/jobs/agent", json={"team_id": FYP1}, headers={"X-Job-Secret": "s"})
    finally:
        app.dependency_overrides.pop(job.get_llm, None)
    assert r.status_code == 200, r.text
    assert r.json()["tools_used"] == ["get_budgets", "get_usage", "forecast_spend", "warn_user"]
    [note] = team_notes(session, FYP1)
    assert OMAR_POD in note.message