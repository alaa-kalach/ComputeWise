"""My two agent tools on the real schema, reading the seeded ledger (FYP2: $50 budget, Lina's pod spent $41)."""
from datetime import datetime, timezone

from app.agent.tools.anomalies import tool_detect_spend_anomalies
from app.agent.tools.forecast import tool_forecast_spend
from app.jobs.agent import discover_tools
from tests.conftest import seed_id

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def test_forecast_reads_the_ledger(session):
    out = tool_forecast_spend(session, str(seed_id("fyp2")), now=NOW)
    assert out["status"] == "ok"
    assert out["balance_usd"] == 50.0 and out["spent_usd"] == 41.0
    assert out["days_elapsed"] == 10 and out["days_left"] == 20
    assert out["will_exceed"] is True and out["days_until_runout"] is not None


def test_forecast_unknown_team_fails_cleanly(session):
    assert tool_forecast_spend(session, "not-a-uuid", now=NOW)["status"] == "failed"
    out = tool_forecast_spend(session, "00000000-0000-0000-0000-000000000000", now=NOW)
    assert out["status"] == "failed"


def test_anomalies_returns_a_list_for_a_real_team(session):
    out = tool_detect_spend_anomalies(session, str(seed_id("fyp2")), now=NOW)
    assert out["status"] == "ok" and isinstance(out["anomalies"], list)
    assert tool_detect_spend_anomalies(session, "nope", now=NOW)["status"] == "failed"


def test_the_agent_finds_both_tools():
    found = discover_tools()
    assert "forecast_spend" in found and "detect_spend_anomalies" in found
