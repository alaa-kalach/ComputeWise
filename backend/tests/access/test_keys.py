"""B3: POST /keys - a student gets his OpenRouter key once, on the mock OpenRouter.

Seed: Sara has approved OpenRouter access and already holds a key in FYP Team 1 and in the
research lab; Omar's FYP Team 1 request was rejected; Jad's (FYP Team 2) is still pending.
For the "first key" tests we approve Omar's request inside the test (it is rolled back after).
"""

from decimal import Decimal

import pytest
from sqlmodel import select

import app.adapters as adapters_pkg
from app.access.pods import available_budget
from app.adapters.mock import WORLD
from app.models import AccessRequest, AuditLog, IssuedResource, Provider, RequestStatus, ResourceStatus
from tests.conftest import auth, seed_id

FYP1, FYP2 = seed_id("fyp1"), seed_id("fyp2")
OMAR = seed_id("u_omar")


@pytest.fixture(autouse=True)
def mock_openrouter(monkeypatch):
    monkeypatch.setattr(adapters_pkg, "adapter_mode", lambda: "mock")
    WORLD.reset()
    yield
    WORLD.reset()


def approve_omar(session):
    req = session.exec(select(AccessRequest).where(
        AccessRequest.user_id == OMAR, AccessRequest.group_id == FYP1,
        AccessRequest.provider == Provider.openrouter)).one()
    req.status = RequestStatus.approved
    session.add(req)
    session.flush()


def issue_audits(session) -> int:
    return len(session.exec(select(AuditLog).where(AuditLog.action == "key.issue")).all())


def get_key(client, who="omar", group=FYP1):
    return client.post("/keys", headers=auth(who), json={"group_id": str(group)})


def test_approved_student_gets_his_key_once(client, session):
    approve_omar(session)
    expected_limit = available_budget(session, FYP1, OMAR)
    before = issue_audits(session)
    r = get_key(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("sk-or-v1-")
    assert Decimal(body["limit_usd"]) == expected_limit and len(body["limit_usd"].split(".")[1]) == 4

    res = session.get(IssuedResource, body["resource_id"])
    assert res.provider == Provider.openrouter and res.user_id == OMAR and res.group_id == FYP1
    assert res.status == ResourceStatus.active and res.current_limit == expected_limit
    # only the hash is stored: the key itself is nowhere in the database
    assert res.external_ref != body["key"] and body["key"] not in str(res.details)
    assert WORLD.resources[res.external_ref].limit_usd == expected_limit

    assert issue_audits(session) == before + 1
    entry = [a for a in session.exec(select(AuditLog).where(AuditLog.action == "key.issue")).all()
             if str(a.target_id) == body["resource_id"]][0]
    assert body["key"] not in str(entry.details)


def test_asking_again_is_refused(client, session):
    approve_omar(session)
    assert get_key(client).status_code == 201
    again = get_key(client)
    assert again.status_code == 409 and "key" not in again.json()


def test_a_key_the_seed_already_issued_is_not_shown_again(client):
    assert get_key(client, "sara").status_code == 409


@pytest.mark.parametrize(
    "who, group, code",
    [
        ("omar", FYP1, 403),  # his request was rejected
        ("jad", FYP2, 403),   # still pending
        ("rania", FYP1, 404),  # outside the group's scope
    ],
)
def test_no_key_without_approved_access(client, session, who, group, code):
    before = issue_audits(session)
    assert get_key(client, who, group).status_code == code
    assert issue_audits(session) == before
