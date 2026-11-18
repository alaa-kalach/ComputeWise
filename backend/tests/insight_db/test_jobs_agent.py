"""C5: POST /jobs/agent on the real schema, with a scripted model and stand-in tools."""
import json
from datetime import date

import pytest

from app.agent.tools_schema import ROLE_TOOLS
from app.jobs import agent as job
from app.main import app
from tests.conftest import seed_id

SECRET = "job-secret-for-tests"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("JOB_SECRET", SECRET)


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


def fake_tools(calls):
    """Stand-ins for every tool a team run may use; they record how they were called."""
    def make(name):
        def tool(session, **kw):
            calls.append((name, kw))
            return {"status": "ok", "tool": name}
        return tool
    return {name: make(name) for name in ROLE_TOOLS["team"] + ROLE_TOOLS["department"]}


@pytest.fixture
def wire(client):
    """Swap the tools and the model of the endpoint for the test."""
    def _wire(tools, llm):
        app.dependency_overrides[job.get_tools] = lambda: tools
        app.dependency_overrides[job.get_llm] = lambda: llm
    yield _wire
    app.dependency_overrides.pop(job.get_tools, None)
    app.dependency_overrides.pop(job.get_llm, None)


H = {"X-Job-Secret": SECRET}


def test_wrong_or_missing_secret_is_refused(client, wire):
    wire(fake_tools([]), ScriptedLLM(["done"]))
    body = {"team_id": str(seed_id("fyp1"))}
    assert client.post("/jobs/agent", json=body).status_code == 401
    assert client.post("/jobs/agent", json=body, headers={"X-Job-Secret": "nope"}).status_code == 401


def test_no_secret_configured_refuses_everything(client, wire, monkeypatch):
    monkeypatch.setenv("JOB_SECRET", "")
    wire(fake_tools([]), ScriptedLLM(["done"]))
    assert client.post("/jobs/agent", json={"team_id": str(seed_id("fyp1"))}, headers=H).status_code == 503


def test_exactly_one_subject(client, wire):
    wire(fake_tools([]), ScriptedLLM(["done"]))
    assert client.post("/jobs/agent", json={}, headers=H).status_code == 422
    both = {"team_id": str(seed_id("fyp1")), "professor_id": str(seed_id("fyp1"))}
    assert client.post("/jobs/agent", json=both, headers=H).status_code == 422


def test_unknown_team_is_404(client, wire):
    wire(fake_tools([]), ScriptedLLM(["done"]))
    r = client.post("/jobs/agent", json={"team_id": "11111111-1111-1111-1111-111111111111"}, headers=H)
    assert r.status_code == 404


def test_missing_tools_give_503_and_name_them(client, wire):
    tools = fake_tools([])
    del tools["get_usage"], tools["warn_user"]
    wire(tools, ScriptedLLM(["done"]))
    r = client.post("/jobs/agent", json={"team_id": str(seed_id("fyp1"))}, headers=H)
    assert r.status_code == 503
    assert set(r.json()["detail"]["missing"]) == {"get_usage", "warn_user"}


def test_a_team_run_calls_the_tools_for_that_team_only(client, wire):
    calls = []
    team = str(seed_id("fyp1"))
    wire(fake_tools(calls), ScriptedLLM([
        ("get_budgets", {"team_id": team}),
        ("get_usage", {"team_id": team}),
        ("forecast_spend", {"team_id": team}),
        "Nothing needs attention.",
    ]))
    r = client.post("/jobs/agent", json={"team_id": team}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["subject"]["id"] == team and out["subject"]["name"]
    assert out["stopped"] == "done" and out["summary"] == "Nothing needs attention."
    assert out["tools_used"] == ["get_budgets", "get_usage", "forecast_spend"]
    assert [c[0] for c in calls] == out["tools_used"]
    assert all(c[1] == {"team_id": team} for c in calls)


def test_a_professor_run_uses_the_department_tools(client, wire):
    calls = []
    prof = str(seed_id("prof_a"))
    wire(fake_tools(calls), ScriptedLLM([("get_professor_budget", {"professor_id": prof}), "All fine."]))
    r = client.post("/jobs/agent", json={"professor_id": prof}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["tools_used"] == ["get_professor_budget"]
    assert calls == [("get_professor_budget", {"professor_id": prof})]


def test_a_tool_that_crashes_does_not_crash_the_run(client, wire):
    tools = fake_tools([])
    def broken(session, **kw):
        raise RuntimeError("database hiccup")
    tools["get_usage"] = broken
    team = str(seed_id("fyp1"))
    wire(tools, ScriptedLLM([("get_usage", {"team_id": team}), "Could not read usage."]))
    r = client.post("/jobs/agent", json={"team_id": team}, headers=H)
    assert r.status_code == 200 and r.json()["stopped"] == "done"


def test_period_is_this_month():
    assert job._period(date(2026, 11, 11)) == {"day": 11, "days": 30}
    assert job._period(date(2026, 2, 3)) == {"day": 3, "days": 28}


def test_default_tools_are_found_by_name_prefix():
    # the placeholder tool files are empty today, so nothing is found yet
    assert isinstance(job.discover_tools(), dict)
