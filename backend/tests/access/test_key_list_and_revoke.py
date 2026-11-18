"""GET /keys and POST /keys/{id}/revoke (C3, C4), as the frontend mocks behave.

Seed: Sara has a key in FYP Team 1 (Prof. A, Karim: usage $8.50, limit $20) and one in the
Research Lab (Prof. B, Rania: usage $35.20, limit $40).
"""

from decimal import Decimal as D

import pytest
from sqlmodel import select

import app.adapters as adapters
from app.adapters import ResourceState
from app.adapters.mock import WORLD, MockResource
from app.models import AuditLog, IssuedResource, ResourceStatus
from tests.conftest import auth, seed_id

FYP1_KEY, LAB_KEY = seed_id("res_sara_fyp1_key"), seed_id("res_sara_lab_key")


@pytest.fixture(autouse=True)
def mock_world(monkeypatch):
    monkeypatch.setattr(adapters, "adapter_mode", lambda: "mock")
    WORLD.reset()
    yield WORLD
    WORLD.reset()


def ids(response):
    return {k["id"] for k in response.json()}


def test_a_student_sees_his_own_keys_never_the_secret(client):
    r = client.get("/keys", headers=auth("sara"))
    assert r.status_code == 200
    keys = {k["id"]: k for k in r.json()}
    assert set(keys) == {str(FYP1_KEY), str(LAB_KEY)}
    fyp1 = keys[str(FYP1_KEY)]
    assert fyp1["group_name"] == "FYP Team 1" and fyp1["user_name"]
    assert (fyp1["status"], fyp1["usage_usd"], fyp1["limit_usd"]) == ("active", "8.5000", "20.0000")
    assert not any("key" == f or "secret" in f for f in fyp1)


def test_a_professor_sees_only_his_groups_keys(client):
    assert ids(client.get("/keys", headers=auth("karim"))) == {str(FYP1_KEY)}
    assert ids(client.get("/keys", headers=auth("rania"))) == {str(LAB_KEY)}


def test_a_student_without_keys_sees_none(client):
    assert client.get("/keys", headers=auth("omar")).json() == []


def test_the_groups_professor_revokes_a_key(client, session):
    r = session.get(IssuedResource, FYP1_KEY)
    WORLD.resources[r.external_ref] = MockResource(provider=r.provider, name="seed", user_id=str(r.user_id),
                                                   group_id=str(r.group_id), total_usd=D("8.5"))
    assert client.post(f"/keys/{FYP1_KEY}/revoke", headers=auth("karim")).status_code == 204

    assert WORLD.resources[r.external_ref].state is ResourceState.disabled
    session.refresh(r)
    assert r.status == ResourceStatus.disabled
    assert session.exec(select(AuditLog).where(AuditLog.action == "key.revoke",
                                               AuditLog.target_id == str(FYP1_KEY))).first()
    listed = {k["id"]: k for k in client.get("/keys", headers=auth("karim")).json()}
    assert listed[str(FYP1_KEY)]["status"] == "disabled"
    assert client.post(f"/keys/{FYP1_KEY}/revoke", headers=auth("karim")).status_code == 409


def test_only_the_groups_professor_may_revoke(client):
    assert client.post(f"/keys/{FYP1_KEY}/revoke", headers=auth("rania")).status_code == 404  # not his group
    assert client.post(f"/keys/{FYP1_KEY}/revoke", headers=auth("sara")).status_code == 403  # the student
    assert client.post(f"/keys/{FYP1_KEY}/revoke", headers=auth("dana")).status_code == 403  # the department


def test_a_pod_is_not_a_key(client):
    assert client.post(f"/keys/{seed_id('res_omar_pod')}/revoke", headers=auth("karim")).status_code == 404