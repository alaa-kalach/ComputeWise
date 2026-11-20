"""get_professor_budget and escalate_to_department on the real schema and the seed data."""
from datetime import datetime, timezone

from sqlmodel import select

from app.agent.tools.department import tool_escalate_to_department, tool_get_professor_budget
from app.insight.escalations import EscalationService  # noqa: F401  (import check)
from app.jobs.agent import discover_tools
from app.models import Escalation
from tests.conftest import seed_id

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
REASON = ("ISSUE: Prof. Karim's teams are close to his $400 ceiling.\n"
          "EVIDENCE: FYP Team 2 has used $41 of $50 and spends about $5.86 a day.\n"
          "PROPOSED ACTION: Raise the ceiling or ask him to move budget between teams.\n"
          "IF IGNORED: FYP Team 2 runs out around day 12 and its pod stops.")


def test_budget_of_a_professor_with_a_ceiling(session):
    out = tool_get_professor_budget(session, str(seed_id("prof_a")), now=NOW)
    assert out["status"] == "ok" and out["name"] == "Prof. Karim Nassar"
    assert out["grant_ceiling_usd"] == 400.0 and out["tracking_only"] is False
    assert out["spent_usd"] == 61.8          # FYP1 20.8 + FYP2 41.0
    assert out["credential"] == "department"
    assert sorted(out["team_ids"]) == sorted([str(seed_id("fyp1")), str(seed_id("fyp2"))])
    assert out["pct_of_ceiling_spent"] == round(61.8 / 400, 3)
    assert out["billable_spent_usd"] == 61.8 and out["observed_spent_usd"] == 0.0


def test_budget_of_a_tracking_only_professor(session):
    out = tool_get_professor_budget(session, str(seed_id("prof_b")), now=NOW)
    assert out["status"] == "ok" and out["tracking_only"] is True
    assert out["grant_ceiling_usd"] is None and out["pct_of_ceiling_spent"] is None
    assert out["will_exceed_ceiling"] is False
    # Mixed: her own OpenRouter key is only observed, but the department pays for her RunPod pod.
    assert out["credential"] == "department"
    assert out["observed_spent_usd"] == 15.2 and out["billable_spent_usd"] == 22.0


def test_unknown_professor_fails_cleanly(session):
    assert tool_get_professor_budget(session, "nope", now=NOW)["status"] == "failed"
    assert tool_get_professor_budget(session, "00000000-0000-0000-0000-000000000000", now=NOW)["status"] == "failed"


def test_escalation_goes_to_the_department_once(session):
    first = tool_escalate_to_department(session, str(seed_id("prof_a")), REASON)
    assert first["status"] == "ok" and first["created"] is True
    again = tool_escalate_to_department(session, str(seed_id("prof_a")), REASON)
    assert again["status"] == "ok" and again["created"] is False       # 24 h cooldown
    rows = session.exec(select(Escalation).where(Escalation.kind == "escalate_to_department")).all()
    assert len(rows) == 1 and rows[0].recipient_type == "department"
    assert rows[0].status == "pending"


def test_escalation_needs_the_four_lines_and_a_real_professor(session):
    assert tool_escalate_to_department(session, str(seed_id("prof_a")), "too short")["status"] == "failed"
    assert tool_escalate_to_department(session, "nope", REASON)["status"] == "failed"


def test_the_agent_finds_both_department_tools():
    found = discover_tools()
    assert "get_professor_budget" in found and "escalate_to_department" in found
