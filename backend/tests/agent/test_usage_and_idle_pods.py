"""Person 1's agent tools on the real schema: get_usage and list_idle_pods.

Seed (now = Nov 10, 12:00): FYP Team 1 spent $8.50 on Sara's key and $12.30 on Omar's running
RTX A4000 pod ($0.17/h); FYP Team 2's only pod (Lina's) is stopped.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent.tools.anomalies import tool_detect_spend_anomalies
from app.agent.tools.idle_pods import (
    idle_hours,
    record_gpu_reading,
    tool_list_idle_pods,
    track_idle_pods,
)
from app.agent.tools.usage import tool_get_usage
from app.agent.tools_schema import ROLE_TOOLS
from app.jobs.agent import discover_tools
from app.models import IssuedResource, ResourceStatus
from tests.conftest import seed_id

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
FYP1, FYP2 = str(seed_id("fyp1")), str(seed_id("fyp2"))
OMAR_POD = str(seed_id("res_omar_pod"))
SARA_KEY = str(seed_id("res_sara_fyp1_key"))


class FakeRunPod:
    """Answers pod_status with whatever GPU utilisation the test sets."""

    def __init__(self, util=0, fail=False):
        self.util, self.fail, self.calls = util, fail, 0

    def pod_status(self, external_ref):
        self.calls += 1
        if self.fail:
            raise RuntimeError("RunPod is down")
        return SimpleNamespace(gpu_util_percent=self.util)


def adapter(fake):
    return lambda credential_id: fake


# ------------------------------------------------------------------ get_usage
def test_usage_lists_each_key_and_pod_with_the_top_drivers(session):
    out = tool_get_usage(session, FYP1, now=NOW)
    assert out["status"] == "ok"
    assert out["spent_usd"] == 20.8 and out["last_7_days_usd"] == 20.8
    rows = {r["id"]: r for r in out["resources"]}
    assert set(rows) == {OMAR_POD, SARA_KEY}
    pod, key = rows[OMAR_POD], rows[SARA_KEY]
    assert pod["type"] == "pod" and pod["spent_usd"] == 12.3 and pod["hourly_usd"] == 0.17
    assert pod["gpu_type"] == "RTX A4000" and pod["label"].endswith("'s RTX A4000 pod")
    assert key["type"] == "key" and key["spent_usd"] == 8.5 and key["limit_usd"] == 20.0
    drivers = out["top_drivers"]
    assert [d["id"] for d in drivers] == [OMAR_POD, SARA_KEY]
    assert drivers[0]["share_of_last_7_days"] == 0.59


def test_quiet_week_falls_back_to_this_months_drivers(session):
    """Spend earlier this month but none in the last 7 days: drivers still name the cause."""
    out = tool_get_usage(session, FYP1, now=NOW + timedelta(days=9))
    assert out["last_7_days_usd"] == 0.0 and out["spent_usd"] == 20.8
    assert out["top_drivers_basis"] == "this_period"
    assert [d["id"] for d in out["top_drivers"]] == [OMAR_POD, SARA_KEY]
    assert out["top_drivers"][0]["share_of_period"] == 0.59


def test_a_normal_week_ranks_by_the_last_7_days(session):
    assert tool_get_usage(session, FYP1, now=NOW)["top_drivers_basis"] == "last_7_days"


def test_usage_uses_the_same_ids_as_the_ledger_and_the_anomaly_tool(session):
    usage_ids = {r["id"] for r in tool_get_usage(session, FYP1, now=NOW)["resources"]}
    # Make Omar's pod spike so the anomaly tool names it, then compare ids.
    from app.models import LedgerEntry, LedgerType, ScopeType
    from decimal import Decimal
    pod = session.get(IssuedResource, seed_id("res_omar_pod"))
    for days_ago, amount in [(3, "-0.5"), (2, "-0.5"), (1, "-0.5"), (0, "-9")]:
        session.add(LedgerEntry(scope_type=ScopeType.user, scope_id=pod.user_id, group_id=pod.group_id,
                                period="2026-11", type=LedgerType.spend, amount=Decimal(amount),
                                provider=pod.provider, credential_id=pod.credential_id, resource_id=pod.id,
                                created_at=NOW - timedelta(days=days_ago, hours=1)))
    session.flush()
    flagged = {a["resource_id"] for a in tool_detect_spend_anomalies(session, FYP1, now=NOW)["anomalies"]}
    assert flagged and flagged <= usage_ids


def test_usage_leaves_out_spend_from_other_teams(session):
    out = tool_get_usage(session, FYP2, now=NOW)
    assert [r["id"] for r in out["resources"]] == [str(seed_id("res_lina_pod"))]
    assert out["spent_usd"] == 41.0


def test_usage_unknown_team_fails_cleanly(session):
    assert tool_get_usage(session, "not-a-uuid", now=NOW)["status"] == "failed"
    assert tool_get_usage(session, "00000000-0000-0000-0000-000000000000", now=NOW)["status"] == "failed"


# ------------------------------------------------------------------ idle history
def _pod(**details):
    return IssuedResource(status=ResourceStatus.running, details=details)


def test_an_idle_reading_starts_the_clock_and_a_busy_one_stops_it():
    pod = _pod()
    record_gpu_reading(pod, 0, NOW)
    record_gpu_reading(pod, 3, NOW + timedelta(hours=10))       # still idle: clock keeps running
    assert idle_hours(pod, NOW + timedelta(hours=30)) == 30.0
    record_gpu_reading(pod, 85, NOW + timedelta(hours=31))      # busy again
    assert idle_hours(pod, NOW + timedelta(hours=32)) == 0.0
    assert record_gpu_reading(pod, None, NOW) is False          # no runtime yet: nothing changes


def test_a_restart_bounds_the_idle_time():
    pod = _pod(gpu_idle_since=NOW.isoformat(), started_at=(NOW + timedelta(hours=20)).isoformat())
    assert idle_hours(pod, NOW + timedelta(hours=22)) == 2.0


# ------------------------------------------------------------------ list_idle_pods
def test_a_pod_idle_for_a_day_is_listed_with_its_cost(session):
    fake = FakeRunPod(util=0)
    assert tool_list_idle_pods(session, FYP1, now=NOW, adapter_for=adapter(fake))["idle_pods"] == []
    later = NOW + timedelta(hours=30)
    out = tool_list_idle_pods(session, FYP1, now=later, adapter_for=adapter(fake))
    assert out["status"] == "ok"
    [pod] = out["idle_pods"]
    assert pod["id"] == OMAR_POD and pod["idle_hours"] == 30.0
    assert pod["hourly_usd"] == 0.17 and pod["idle_cost_usd"] == 5.1


def test_a_working_pod_is_not_listed(session):
    fake = FakeRunPod(util=0)
    tool_list_idle_pods(session, FYP1, now=NOW, adapter_for=adapter(fake))
    fake.util = 90
    out = tool_list_idle_pods(session, FYP1, now=NOW + timedelta(hours=30), adapter_for=adapter(fake))
    assert out["idle_pods"] == []


def test_runpod_down_keeps_the_history(session):
    tool_list_idle_pods(session, FYP1, now=NOW, adapter_for=adapter(FakeRunPod(util=0)))
    out = tool_list_idle_pods(session, FYP1, now=NOW + timedelta(hours=26),
                              adapter_for=adapter(FakeRunPod(fail=True)))
    assert [p["id"] for p in out["idle_pods"]] == [OMAR_POD]


def test_stopped_pods_are_never_idle(session):
    fake = FakeRunPod(util=0)
    out = tool_list_idle_pods(session, FYP2, now=NOW, adapter_for=adapter(fake))
    assert out == {"status": "ok", "team_id": FYP2, "idle_threshold_hours": 24, "idle_pods": []}
    assert fake.calls == 0


def test_sync_tracker_reads_every_running_pod(session):
    fake = FakeRunPod(util=0)
    assert track_idle_pods(session, NOW, adapter_for=adapter(fake)) == {"read": 2, "failed": 0}  # Omar, Maya


def test_list_idle_pods_unknown_team_fails_cleanly(session):
    assert tool_list_idle_pods(session, "nope", now=NOW)["status"] == "failed"


# ------------------------------------------------------------------ wiring
def test_the_agent_no_longer_misses_my_tools():
    found = discover_tools()
    assert "get_usage" in found and "list_idle_pods" in found
    for role in ("team", "department"):
        missing = [t for t in ROLE_TOOLS[role] if t not in found]
        assert "get_usage" not in missing and "list_idle_pods" not in missing
