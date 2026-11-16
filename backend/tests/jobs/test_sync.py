"""The sync job v1 on the mock adapters. The Week 2 "done when": the sync job turns mock usage
into ledger entries, each with its funding credential.

The seed's last snapshots match its ledger: Omar's pod has a total of $12.30, Sara's
Research Lab key $35.20. The mock providers start empty, so a test first puts the
resources it needs into the mock WORLD with a new total.
"""

from decimal import Decimal as D

import pytest
from sqlmodel import select

import app.adapters as adapters
from app.adapters.mock import WORLD, MockResource
from app.budget.api import current_period
from app.core.config import get_settings
from app.jobs.sync import run_sync
from app.models import IssuedResource, LedgerEntry, LedgerType, Provider, UsageSnapshot
from tests.conftest import auth, seed_id

OMAR_POD, SARA_LAB_KEY = seed_id("res_omar_pod"), seed_id("res_sara_lab_key")
FYP1 = seed_id("fyp1")
SECRET = "test-job-secret"


@pytest.fixture(autouse=True)
def mock_world(monkeypatch):
    monkeypatch.setattr(adapters, "adapter_mode", lambda: "mock")  # never call a real provider
    monkeypatch.setattr(get_settings(), "job_secret", SECRET)
    WORLD.reset()
    yield WORLD
    WORLD.reset()


def at_provider(session, resource_id, total):
    """Make the mock provider report `total` USD spent so far for this seed resource."""
    r = session.get(IssuedResource, resource_id)
    WORLD.resources[r.external_ref] = MockResource(
        provider=r.provider, name="seed", user_id=str(r.user_id), group_id=str(r.group_id), total_usd=D(total),
    )


def spend_lines(session, resource_id):
    return list(session.exec(select(LedgerEntry).where(
        LedgerEntry.resource_id == resource_id, LedgerEntry.type == LedgerType.spend,
        LedgerEntry.period == current_period(),
    ).order_by(LedgerEntry.created_at)).all())


def snapshots(session, resource_id):
    return list(session.exec(select(UsageSnapshot).where(UsageSnapshot.issued_resource_id == resource_id)).all())


def test_new_usage_becomes_a_spend_line_with_its_funding_credential(session):
    at_provider(session, OMAR_POD, "17.3")  # $5 more than the last snapshot
    result = run_sync(session)
    assert result.spend_entries == 1 and not result.skipped
    last = spend_lines(session, OMAR_POD)[-1]
    assert last.amount == D("-5")
    assert (last.provider, last.credential_id) == (Provider.runpod, seed_id("cred_dept_rp"))  # billable
    assert snapshots(session, OMAR_POD)[-1].cumulative_usd == D("17.3")


def test_observed_spend_keeps_the_professors_own_credential(session):
    at_provider(session, SARA_LAB_KEY, "40.2")
    run_sync(session)
    assert spend_lines(session, SARA_LAB_KEY)[-1].credential_id == seed_id("cred_profb_or")


def test_running_it_twice_writes_spend_once(session):
    at_provider(session, OMAR_POD, "17.3")
    run_sync(session)
    before = len(spend_lines(session, OMAR_POD))
    assert run_sync(session).spend_entries == 0
    assert len(spend_lines(session, OMAR_POD)) == before


def test_new_spend_shows_in_the_team_budget(client, session):
    at_provider(session, OMAR_POD, "17.3")
    run_sync(session)
    team = client.get(f"/groups/{FYP1}/budget", headers=auth("karim")).json()["team"]
    assert team["spent"] == "25.8000"  # 20.80 + 5


def test_a_lower_total_is_never_negative_spend(session):
    at_provider(session, OMAR_POD, "10")  # below the last snapshot of 12.30
    assert run_sync(session).spend_entries == 0


def test_unknown_at_the_provider_writes_nothing(session):
    before = len(snapshots(session, OMAR_POD))
    result = run_sync(session)  # the mock world is empty: every seed resource is "gone"
    assert result.spend_entries == 0 and result.resources_polled == 0
    assert len(snapshots(session, OMAR_POD)) == before


def test_a_provider_outage_is_skipped_not_fatal(session):
    at_provider(session, OMAR_POD, "17.3")
    WORLD.outage = True
    result = run_sync(session)
    assert result.spend_entries == 0 and result.errors


# ---------------------------------------------------------------- POST /jobs/sync
def test_endpoint_needs_the_job_secret(client):
    assert client.post("/jobs/sync").status_code == 401
    assert client.post("/jobs/sync", headers={"X-Job-Secret": "wrong"}).status_code == 401
    assert client.post("/jobs/sync", headers=auth("dana")).status_code == 401  # a login is not enough


def test_endpoint_runs_the_sync(client, session):
    at_provider(session, OMAR_POD, "17.3")
    r = client.post("/jobs/sync", headers={"X-Job-Secret": SECRET})
    assert r.status_code == 200
    assert r.json() == {"resources_polled": 1, "spend_entries": 1, "skipped": False}


def test_no_secret_configured_means_the_job_is_off(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_secret", "")
    assert client.post("/jobs/sync", headers={"X-Job-Secret": ""}).status_code == 401


def test_a_second_run_while_one_is_running_is_skipped(engine, session):
    from sqlalchemy import text

    from app.jobs.sync import SYNC_LOCK_ID

    with engine.connect() as other:  # another sync, still running, holds the lock
        other.execute(text("SELECT pg_advisory_lock(:id)"), {"id": SYNC_LOCK_ID})
        try:
            assert run_sync(session).skipped is True
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": SYNC_LOCK_ID})