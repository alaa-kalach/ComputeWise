"""Week 3: the sync job enforces budgets (B5, B6), on the mock adapters.

The plan's "done when":
  - calling the sync twice in a row writes spend once
  - a mock team that crosses 100% has its pod stopped within one sync cycle
  - a stopped pod's storage appears in the team's spend
plus the rest of B6: the 80% warning, capped keys, key limits recalculated from the team
balance, a top-up re-opening a capped team, and the revocation cascade.

Seed (see scripts/seed.py), this month:
  FYP Team 1   $100 (50 + 50 rollover), spent $20.80. Omar's pod (running, $0.17/h, user
               budget $25, spent 12.30) and Sara's key (usage 8.50, user budget $20).
  FYP Team 2   $50, spent $41 (82%). Lina's pod is stopped (storage $0.0056/h).
  Research Lab $152, spent $37.20. Maya's pod (running), Sara's key (usage 35.20, user
               budget $40, spent 15.20) under Prof. B's own OpenRouter credential.
Every seed resource's last usage snapshot is at 2026-11-10 10:00 UTC.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlmodel import select

import app.adapters as adapters
from app.adapters import ResourceState
from app.adapters.mock import WORLD, MockResource
from app.core.config import get_settings
from app.jobs.sync import run_sync
from app.models import (
    AuditLog,
    CredentialGrant,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    Notification,
    ResourceStatus,
)
from tests.conftest import auth, seed_id

OMAR_POD, LINA_POD, MAYA_POD = seed_id("res_omar_pod"), seed_id("res_lina_pod"), seed_id("res_maya_pod")
SARA_FYP1_KEY, SARA_LAB_KEY = seed_id("res_sara_fyp1_key"), seed_id("res_sara_lab_key")
FYP1, FYP2 = seed_id("fyp1"), seed_id("fyp2")
DEPT_RUNPOD, PROF_B_OPENROUTER = seed_id("cred_dept_rp"), seed_id("cred_profb_or")
LAST_SNAPSHOT = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def mock_world(monkeypatch):
    monkeypatch.setattr(adapters, "adapter_mode", lambda: "mock")  # never call a real provider
    monkeypatch.setattr(get_settings(), "job_secret", "test-job-secret")
    WORLD.reset()
    yield WORLD
    WORLD.reset()


def at_provider(session, resource_id, total=None, *, state=ResourceState.active, billed=None):
    """Put a seed resource into the mock provider. `total` defaults to its last snapshot,
    so nothing new was spent; the pod's prices come from its seed details."""
    r = session.get(IssuedResource, resource_id)
    if total is None:
        total = {OMAR_POD: "12.3", LINA_POD: "91", MAYA_POD: "62", SARA_FYP1_KEY: "8.5", SARA_LAB_KEY: "35.2"}[resource_id]
    d = r.details or {}
    WORLD.resources[r.external_ref] = MockResource(
        provider=r.provider, name="seed", user_id=str(r.user_id), group_id=str(r.group_id),
        state=state, total_usd=D(total), billed_usd=D(billed if billed is not None else total),
        limit_usd=r.current_limit,
        cost_per_hr=D(str(d.get("cost_per_hr", 0))), storage_cost_per_hr=D(str(d.get("storage_cost_per_hr", 0))),
    )
    return WORLD.resources[r.external_ref]


def spend_since_seed(session, resource_id):
    """New spend the sync wrote for this resource (the seed's own lines are 2 h before its 'now')."""
    rows = session.exec(select(LedgerEntry).where(
        LedgerEntry.resource_id == resource_id, LedgerEntry.type == LedgerType.spend, LedgerEntry.created_by.is_(None),
        LedgerEntry.created_at > datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc),
    )).all()
    return -sum((e.amount for e in rows), D(0))


def resource(session, resource_id):
    r = session.get(IssuedResource, resource_id)
    session.refresh(r)
    return r


def notifications(session, group_id):
    return list(session.exec(select(Notification).where(Notification.group_id == group_id)).all())


def audits(session, action, target_id):
    return list(session.exec(select(AuditLog).where(AuditLog.action == action, AuditLog.target_id == str(target_id))).all())


# ====================================================================== B5: RunPod spend is estimated (D15)
def test_lagging_billing_running_pod_is_charged_by_the_hour(session):
    WORLD.lagging_billing = True
    at_provider(session, OMAR_POD, billed="12.3")  # RunPod has billed nothing new yet
    run_sync(session, now=LAST_SNAPSHOT + timedelta(hours=1))
    assert spend_since_seed(session, OMAR_POD) == D("0.17")  # 1 h x $0.17


def test_a_stopped_pods_storage_appears_in_the_team_spend(session):
    WORLD.lagging_billing = True
    at_provider(session, LINA_POD, state=ResourceState.disabled)
    run_sync(session, now=LAST_SNAPSHOT + timedelta(hours=10))
    assert spend_since_seed(session, LINA_POD) == D("0.056")  # 10 h x $0.0056 of disk
    assert resource(session, LINA_POD).status == ResourceStatus.stopped


def test_storage_also_counts_when_the_provider_reports_it_live(session):
    pod = at_provider(session, LINA_POD, state=ResourceState.disabled)
    WORLD.advance(hours=10)  # the mock charges disk storage on stopped pods
    assert pod.total_usd == D("91.056")
    run_sync(session)
    assert spend_since_seed(session, LINA_POD) == D("0.056")


def test_the_estimate_never_records_less_than_the_bill(session):
    WORLD.lagging_billing = True
    at_provider(session, OMAR_POD, total="20", billed="20")  # billing caught up, and is higher
    run_sync(session, now=LAST_SNAPSHOT + timedelta(hours=1))
    assert spend_since_seed(session, OMAR_POD) == D("7.7")  # 20 - 12.30, not 0.17


def test_twice_in_a_row_writes_spend_once_with_estimates_too(session):
    WORLD.lagging_billing = True
    at_provider(session, OMAR_POD)
    now = LAST_SNAPSHOT + timedelta(hours=1)
    run_sync(session, now=now)
    assert run_sync(session, now=now).spend_entries == 0
    assert spend_since_seed(session, OMAR_POD) == D("0.17")


def test_a_pod_runpod_stopped_on_its_own_is_recorded_as_stopped(session):
    at_provider(session, OMAR_POD, state=ResourceState.disabled)
    run_sync(session)
    pod = resource(session, OMAR_POD)
    assert pod.status == ResourceStatus.stopped and "stopped_by" not in pod.details
    assert audits(session, "pod.state_sync", OMAR_POD)[-1].details["by"] == "provider"


def test_the_pod_list_still_works_after_runpod_stopped_a_pod(client, session):
    at_provider(session, OMAR_POD, state=ResourceState.disabled)
    run_sync(session)
    r = client.get("/pods", headers=auth("karim"))
    assert r.status_code == 200
    (omar,) = [p for p in r.json() if p["id"] == str(OMAR_POD)]
    assert omar["state"] == "stopped" and omar["stopped_by"] is None


# ====================================================================== B6: 100%
def test_a_team_that_crosses_100_percent_has_its_pod_stopped_within_one_sync(session):
    pod = at_provider(session, OMAR_POD, "92.3")  # +$80: FYP Team 1 has spent $100.80 of $100
    key = at_provider(session, SARA_FYP1_KEY)
    result = run_sync(session)

    assert pod.state is ResourceState.disabled  # stopped at RunPod...
    db_pod = resource(session, OMAR_POD)
    assert db_pod.status == ResourceStatus.stopped  # ...never terminated (D3)
    assert db_pod.details["stopped_by"] == "watchdog"
    assert audits(session, "pod.stop", OMAR_POD)[-1].details["reason"] == "team budget used up"

    assert key.limit_usd == D("8.5")  # capped at what it has used
    assert resource(session, SARA_FYP1_KEY).status == ResourceStatus.capped
    assert audits(session, "key.cap", SARA_FYP1_KEY)

    (alert,) = [n for n in notifications(session, FYP1) if n.level == "critical"]
    assert "used all" in alert.message
    assert result.watchdog.pods_stopped == 1 and result.watchdog.keys_capped == 1


def test_a_user_out_of_his_own_budget_has_only_his_pod_stopped(session):
    pod = at_provider(session, OMAR_POD, "25.3")  # Omar: $25.30 of his $25; the team is fine
    run_sync(session)
    assert pod.state is ResourceState.disabled
    assert audits(session, "pod.stop", OMAR_POD)[-1].details["reason"] == "user budget used up"
    assert notifications(session, FYP1) == []  # the team itself is at 34%


def test_a_top_up_reopens_a_capped_key(client, session):
    at_provider(session, OMAR_POD, "92.3")
    key = at_provider(session, SARA_FYP1_KEY)
    run_sync(session)
    assert resource(session, SARA_FYP1_KEY).status == ResourceStatus.capped

    r = client.post(f"/groups/{FYP1}/top-ups", json={"amount": "50", "note": "Extra for the demo"}, headers=auth("karim"))
    assert r.status_code == 201
    result = run_sync(session)

    assert resource(session, SARA_FYP1_KEY).status == ResourceStatus.active
    assert key.limit_usd == D("20")  # 8.50 used + her own $11.50 left (less than the team's $49.20)
    assert result.watchdog.keys_reopened == 1
    assert resource(session, OMAR_POD).status == ResourceStatus.stopped  # pods stay stopped; he restarts it


def test_a_topped_up_team_that_runs_out_again_is_told_again(client, session):
    at_provider(session, OMAR_POD, "92.3")
    run_sync(session)
    client.post(f"/groups/{FYP1}/top-ups", json={"amount": "10", "note": "A little more"}, headers=auth("karim"))
    run_sync(session)  # back under 100%: no new alert
    WORLD.resources["seedpod-omar-fyp1"].total_usd = D("102.3")  # spends the $10 too
    run_sync(session)
    assert len([n for n in notifications(session, FYP1) if n.level == "critical"]) == 2


# ====================================================================== B6: 80%
def test_a_team_past_80_percent_is_warned_once(session):
    run_sync(session)
    run_sync(session)
    (warning,) = notifications(session, FYP2)  # FYP Team 2: $41 of $50
    assert warning.level == "warning"
    assert "82%" in warning.message and "$9.00 left" in warning.message
    assert len(audits(session, "budget.warn", FYP2)) == 1


def test_a_team_under_80_percent_is_not_warned(session):
    run_sync(session)
    assert notifications(session, FYP1) == []


# ====================================================================== B5: key limits follow the team balance
def test_a_key_limit_is_its_usage_plus_what_is_left(session):
    key = at_provider(session, SARA_LAB_KEY)  # seed limit $40
    run_sync(session)
    # 35.20 used + min(team left $114.80, Sara's own $24.80 left) = $60
    assert key.limit_usd == D("60")
    assert resource(session, SARA_LAB_KEY).current_limit == D("60")
    assert audits(session, "key.set_limit", SARA_LAB_KEY)


def test_an_unchanged_limit_is_not_sent_again(session):
    at_provider(session, SARA_LAB_KEY)
    run_sync(session)
    assert run_sync(session).watchdog.limits_set == 0


def test_a_provider_outage_during_enforcement_is_retried_next_time(session):
    pod = at_provider(session, OMAR_POD, "92.3")
    run_sync(session)  # records the spend and stops the pod
    pod.state = ResourceState.active  # pretend the stop never reached RunPod
    s = resource(session, OMAR_POD)
    s.status = ResourceStatus.running
    session.add(s)
    session.commit()
    WORLD.outage = True
    assert run_sync(session).errors
    WORLD.outage = False
    run_sync(session)
    assert pod.state is ResourceState.disabled


# ====================================================================== B6: revocation cascade
def test_revoking_a_credential_stops_its_pods_at_once(client, session):
    omar, maya = at_provider(session, OMAR_POD), at_provider(session, MAYA_POD)
    assert client.delete(f"/credentials/{DEPT_RUNPOD}", headers=auth("dana")).status_code == 204

    assert omar.state is ResourceState.disabled and maya.state is ResourceState.disabled
    for pod_id in (OMAR_POD, MAYA_POD):
        pod = resource(session, pod_id)
        assert pod.status == ResourceStatus.stopped and "stopped_by" not in pod.details
        stop = audits(session, "pod.stop", pod_id)[-1]
        assert stop.actor_id == seed_id("u_dana") and stop.details["by"] == "revocation"
    assert resource(session, LINA_POD).status == ResourceStatus.stopped  # already stopped: untouched


def test_the_pod_list_still_works_after_a_revocation(client, session):
    at_provider(session, OMAR_POD)
    client.delete(f"/credentials/{DEPT_RUNPOD}", headers=auth("dana"))
    r = client.get("/pods", headers=auth("karim"))
    assert r.status_code == 200
    (omar,) = [p for p in r.json() if p["id"] == str(OMAR_POD)]
    assert omar["state"] == "stopped" and omar["stopped_by"] is None


def test_revoking_one_grant_only_cuts_that_professors_groups(client, session):
    omar, maya = at_provider(session, OMAR_POD), at_provider(session, MAYA_POD)
    grant = session.exec(select(CredentialGrant).where(
        CredentialGrant.credential_id == DEPT_RUNPOD, CredentialGrant.professor_id == seed_id("prof_b"))).one()
    r = client.delete(f"/credentials/{DEPT_RUNPOD}/grants/{grant.id}", headers=auth("dana"))
    assert r.status_code == 204
    assert maya.state is ResourceState.disabled  # Prof. B's Research Lab
    assert omar.state is ResourceState.active  # Prof. A still has his grant


def test_revoking_an_openrouter_credential_disables_its_keys(client, session):
    key = at_provider(session, SARA_LAB_KEY)
    assert client.delete(f"/credentials/{PROF_B_OPENROUTER}", headers=auth("rania")).status_code == 204
    assert key.state is ResourceState.disabled
    assert resource(session, SARA_LAB_KEY).status == ResourceStatus.disabled
    assert audits(session, "key.disable", SARA_LAB_KEY)


def test_a_provider_down_at_revoke_time_is_cut_off_by_the_next_sync(client, session):
    omar = at_provider(session, OMAR_POD)
    WORLD.outage = True
    assert client.delete(f"/credentials/{DEPT_RUNPOD}", headers=auth("dana")).status_code == 204
    assert resource(session, OMAR_POD).status == ResourceStatus.running  # RunPod was unreachable
    WORLD.outage = False
    result = run_sync(session)
    assert omar.state is ResourceState.disabled
    assert resource(session, OMAR_POD).status == ResourceStatus.stopped
    assert result.watchdog.cut_off >= 1