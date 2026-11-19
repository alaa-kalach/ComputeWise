"""C6: the department admin's "Sync now" button, POST /department/sync.

Same sync as pg_cron's POST /jobs/sync, but with a login instead of the job secret, and only
for department admins. The mock providers start empty, so the test puts Omar's pod there.
"""

from decimal import Decimal as D

import pytest
from sqlmodel import select

import app.adapters as adapters
from app.adapters.mock import WORLD, MockResource
from app.models import AuditLog, IssuedResource
from tests.conftest import auth, seed_id

OMAR_POD = seed_id("res_omar_pod")


@pytest.fixture(autouse=True)
def mock_world(monkeypatch):
    monkeypatch.setattr(adapters, "adapter_mode", lambda: "mock")  # never call a real provider
    WORLD.reset()
    yield WORLD
    WORLD.reset()


def test_the_department_admin_runs_a_sync_and_it_is_audited(client, session):
    r = session.get(IssuedResource, OMAR_POD)
    WORLD.resources[r.external_ref] = MockResource(
        provider=r.provider, name="seed", user_id=str(r.user_id), group_id=str(r.group_id), total_usd=D("17.3"),
    )
    res = client.post("/department/sync", headers=auth("dana"))
    assert res.status_code == 200
    body = res.json()
    # errors counts credentials the (empty) mock provider did not answer for; not this test's business.
    assert {k: body[k] for k in ("resources_polled", "spend_entries", "skipped")} == {
        "resources_polled": 1, "spend_entries": 1, "skipped": False}

    entry = session.exec(select(AuditLog).where(AuditLog.action == "sync.manual")).one()
    assert entry.target_type == "department" and entry.details["spend_entries"] == 1


def test_a_second_click_writes_no_new_spend(client, session):
    r = session.get(IssuedResource, OMAR_POD)
    WORLD.resources[r.external_ref] = MockResource(
        provider=r.provider, name="seed", user_id=str(r.user_id), group_id=str(r.group_id), total_usd=D("17.3"),
    )
    assert client.post("/department/sync", headers=auth("dana")).json()["spend_entries"] == 1
    assert client.post("/department/sync", headers=auth("dana")).json()["spend_entries"] == 0


@pytest.mark.parametrize("who", ["karim", "rania", "sara"])
def test_only_the_department_admin_can_sync(client, who):
    assert client.post("/department/sync", headers=auth(who)).status_code == 403


def test_a_login_is_needed(client):
    assert client.post("/department/sync").status_code == 401
