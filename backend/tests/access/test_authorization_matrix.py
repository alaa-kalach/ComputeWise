"""A8: authorization over HTTP, endpoint by endpoint, for every kind of caller.

The design's rules (Security and system rules):
  * Anything outside the caller's scope answers 404, so ids of other professors' objects don't leak.
  * Only a group's own professor approves access, issues or revokes keys, controls pods and changes
    budgets or members. Inside your scope without that role you get 403.
  * The department admin observes (reads groups and budgets) but never manages a group unless he
    also holds the professor role for it.
  * A refused call changes nothing: no ledger entry, no audit entry, no status change.

test_scoping.py checks the shared scope helper itself; this file checks that every endpoint uses it.
Seed: Karim (Prof A) owns FYP Team 1 and 2, Rania (Prof B) owns Research Lab, Sara is in FYP 1 and
the Lab, Omar in FYP 1, Jad in FYP 2, Maya in the Lab, Dana is the department admin only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func
from sqlmodel import select

import app.adapters as adapters
from app.models import (
    AccessRequest,
    AuditLog,
    BudgetPolicy,
    Escalation,
    Group,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    Notification,
    RequestStatus,
)
from tests.conftest import auth, seed_id

ESCALATION_LEAK = pytest.mark.xfail(
    strict=True,
    reason="Person 3 (insight/api.py): someone else's escalation answers 403 but an unknown id 404, so "
    "the status code reveals that the id exists. The design says 404 outside scope. When it is "
    "changed, this case passes and strict xfail fails: remove the marker then.",
)

# (who, method, path, body, expected status). {names} are filled in by the `ids` fixture.
OUTSIDE_SCOPE = [  # -> 404: the caller must not learn that the object exists
    # A professor reaching into another professor's group
    ("karim", "GET", "/groups/{lab}", None),
    ("karim", "PATCH", "/groups/{lab}", {"name": "Taken over"}),
    ("karim", "GET", "/groups/{lab}/budget", None),
    ("karim", "PUT", "/groups/{lab}/budget-policy", {"monthly_amount": "10", "rollover": True}),
    ("karim", "POST", "/groups/{lab}/top-ups", {"amount": "5", "note": "x"}),
    ("karim", "POST", "/groups/{lab}/corrections", {"amount": "-5", "note": "x"}),
    ("karim", "GET", "/groups/{lab}/member-budgets", None),
    ("karim", "POST", "/groups/{lab}/members", {"email": "new@students.example.edu", "name": "New"}),
    ("karim", "PATCH", "/groups/{lab}/members/{maya}", {"status": "suspended"}),
    ("karim", "DELETE", "/groups/{lab}/members/{maya}", None),
    ("karim", "PUT", "/groups/{lab}/members/{maya}/budget-policy", {"monthly_amount": "5"}),
    ("karim", "GET", "/pods/{maya_pod}", None),
    ("karim", "POST", "/pods/{maya_pod}/stop", None),
    ("karim", "POST", "/pods/{maya_pod}/start", None),
    ("karim", "DELETE", "/pods/{maya_pod}", None),
    ("karim", "POST", "/keys/{sara_lab_key}/revoke", None),
    ("karim", "POST", "/access-requests/{req_lab}/approve", None),
    ("karim", "POST", "/access-requests/{req_lab}/reject", {"reason": "x"}),
    ("karim", "GET", "/runpod/gpus?group_id={lab}", None),
    ("karim", "DELETE", "/credentials/{cred_profb_or}", None),
    ("karim", "PUT", "/professors/{prof_b}/budget-setting", {"mode": "tracking", "monthly_amount": None}),
    ("karim", "POST", "/notifications/{note_lab}/read", None),
    pytest.param("karim", "POST", "/escalations/{esc_lab}/approve", None, marks=ESCALATION_LEAK),
    pytest.param("karim", "POST", "/escalations/{esc_lab}/reject", {"note": "x"}, marks=ESCALATION_LEAK),
    # A student reaching into a group he is not in
    ("jad", "GET", "/groups/{fyp1}", None),
    ("jad", "GET", "/groups/{fyp1}/budget", None),
    ("jad", "GET", "/pods/{omar_pod}", None),
    ("jad", "POST", "/pods/{omar_pod}/stop", None),
    ("jad", "GET", "/runpod/gpus?group_id={fyp1}", None),
    ("jad", "POST", "/keys", {"group_id": "{fyp1}"}),
    ("jad", "POST", "/access-requests", {"group_id": "{fyp1}", "provider": "openrouter"}),
    ("jad", "POST", "/pods", {"group_id": "{fyp1}", "gpu_type_id": "NVIDIA RTX A4000", "cloud": "community"}),
    ("jad", "POST", "/notifications/{note_lab}/read", None),
    # A student touching a teammate's pod: other people's pods are not his to see
    ("sara", "GET", "/pods/{omar_pod}", None),
    ("sara", "POST", "/pods/{omar_pod}/stop", None),
    ("sara", "POST", "/access-requests/{req_fyp2}/approve", None),
]

NOT_YOUR_ROLE = [  # -> 403: inside the caller's scope, but only the group's professor may do it
    # A student in her own group
    ("sara", "PATCH", "/groups/{fyp1}", {"name": "Ours now"}),
    ("sara", "PUT", "/groups/{fyp1}/budget-policy", {"monthly_amount": "999", "rollover": True}),
    ("sara", "POST", "/groups/{fyp1}/top-ups", {"amount": "500", "note": "more please"}),
    ("sara", "POST", "/groups/{fyp1}/corrections", {"amount": "500", "note": "x"}),
    ("sara", "GET", "/groups/{fyp1}/member-budgets", None),
    ("sara", "POST", "/groups/{fyp1}/members", {"email": "friend@students.example.edu", "name": "Friend"}),
    ("sara", "PATCH", "/groups/{fyp1}/members/{omar}", {"status": "suspended"}),
    ("sara", "PUT", "/groups/{fyp1}/members/{sara}/budget-policy", {"monthly_amount": "500"}),  # raise her own limit
    ("sara", "POST", "/keys/{sara_fyp1_key}/revoke", None),
    # ... and nothing of the department's or a professor's
    ("sara", "POST", "/professors", {"email": "x@cs.example.edu", "name": "X"}),
    ("sara", "POST", "/groups", {"name": "My own team"}),
    ("sara", "GET", "/budget/summary", None),
    ("sara", "GET", "/reports/billing?period=2026-11", None),
    ("sara", "GET", "/reports/trend", None),
    ("sara", "GET", "/reports/weekly", None),
    ("sara", "POST", "/department/sync", None),
    # The department admin without the professor role: observes, never manages a group
    ("dana", "PATCH", "/groups/{fyp1}", {"name": "x"}),
    ("dana", "PUT", "/groups/{fyp1}/budget-policy", {"monthly_amount": "10", "rollover": True}),
    ("dana", "POST", "/groups/{fyp1}/top-ups", {"amount": "5", "note": "x"}),
    ("dana", "POST", "/groups/{fyp1}/corrections", {"amount": "-5", "note": "x"}),
    ("dana", "POST", "/groups/{fyp1}/members", {"email": "x@students.example.edu", "name": "X"}),
    ("dana", "PATCH", "/groups/{fyp1}/members/{omar}", {"status": "suspended"}),
    ("dana", "PUT", "/groups/{fyp1}/members/{omar}/budget-policy", {"monthly_amount": "5"}),
    ("dana", "POST", "/access-requests/{req_fyp2}/approve", None),
    ("dana", "POST", "/access-requests/{req_fyp2}/reject", {"reason": "x"}),
    ("dana", "POST", "/pods/{omar_pod}/stop", None),
    ("dana", "POST", "/pods/{lina_pod}/start", None),
    ("dana", "DELETE", "/pods/{omar_pod}", None),
    ("dana", "POST", "/keys/{sara_fyp1_key}/revoke", None),
    ("dana", "POST", "/keys", {"group_id": "{fyp1}"}),
    ("dana", "POST", "/groups", {"name": "Dana's team"}),
    ("dana", "POST", "/escalations/{esc_lab}/approve", None),  # a professor's decision, not hers
]


@pytest.fixture(autouse=True)
def mock_providers(monkeypatch):
    monkeypatch.setattr(adapters, "adapter_mode", lambda: "mock")  # never a real provider


@pytest.fixture
def ids(session) -> dict[str, str]:
    """Seed ids by short name, plus a decidable request, an escalation and a notification in the Lab."""
    pending_fyp2 = session.exec(
        select(AccessRequest).where(AccessRequest.status == RequestStatus.pending)
    ).one()  # Jad's OpenRouter request in FYP 2
    lab_request = session.exec(
        select(AccessRequest).where(AccessRequest.group_id == seed_id("research_lab"))
    ).first()
    lab_request.status = RequestStatus.pending  # so only scope can refuse it, not "already decided"
    escalation = Escalation(
        kind="propose_pod_stop", target=str(seed_id("res_maya_pod")), reason="ISSUE: idle pod",
        recipient_type="professor", recipient_id=seed_id("prof_b"), group_id=seed_id("research_lab"),
    )
    note = Notification(group_id=seed_id("research_lab"), message="Research Lab is at 80%")
    session.add_all([lab_request, escalation, note])
    session.flush()
    names = {
        "lab": "research_lab", "fyp1": "fyp1", "maya": "u_maya", "omar": "u_omar", "sara": "u_sara",
        "maya_pod": "res_maya_pod", "omar_pod": "res_omar_pod", "lina_pod": "res_lina_pod",
        "sara_lab_key": "res_sara_lab_key", "sara_fyp1_key": "res_sara_fyp1_key",
        "cred_profb_or": "cred_profb_or", "prof_b": "prof_b",
    }
    return {
        **{k: str(seed_id(v)) for k, v in names.items()},
        "req_lab": str(lab_request.id), "req_fyp2": str(pending_fyp2.id),
        "esc_lab": str(escalation.id), "note_lab": str(note.id),
    }


def fill(value, ids):
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {k: fill(v, ids) for k, v in value.items()}
    return value


def snapshot(session) -> tuple:
    """Everything a refused call must leave as it was."""
    session.expire_all()
    count = lambda model: session.exec(select(func.count()).select_from(model)).one()  # noqa: E731
    return (
        count(LedgerEntry),
        count(AuditLog),
        count(GroupMember),
        count(Group),
        count(IssuedResource),
        sorted((str(r.id), r.status.value) for r in session.exec(select(IssuedResource)).all()),
        sorted((str(r.id), r.status.value) for r in session.exec(select(AccessRequest)).all()),
        sorted((str(m.group_id), str(m.user_id), m.status.value) for m in session.exec(select(GroupMember)).all()),
        sorted((str(g.id), g.name) for g in session.exec(select(Group)).all()),
        sorted((str(p.id), str(p.monthly_amount)) for p in session.exec(select(BudgetPolicy)).all()),
        sorted((str(e.id), e.status) for e in session.exec(select(Escalation)).all()),
    )


def _call(client, ids, who, method, path, body):
    return client.request(method, fill(path, ids), headers=auth(who), json=fill(body, ids))


def _signed_in(client, who):
    """A first login links the token to the seed user and audits that (A2). Do it before the
    snapshot, so the snapshot measures only the refused call."""
    assert client.get("/me", headers=auth(who)).status_code == 200


@pytest.mark.parametrize("who, method, path, body", OUTSIDE_SCOPE)
def test_outside_your_scope_is_not_found_and_changes_nothing(client, session, ids, who, method, path, body):
    _signed_in(client, who)
    before = snapshot(session)
    r = _call(client, ids, who, method, path, body)
    assert r.status_code == 404, f"{who} {method} {path}: {r.status_code} {r.text[:120]}"
    assert snapshot(session) == before


@pytest.mark.parametrize("who, method, path, body", NOT_YOUR_ROLE)
def test_not_your_role_is_forbidden_and_changes_nothing(client, session, ids, who, method, path, body):
    _signed_in(client, who)
    before = snapshot(session)
    r = _call(client, ids, who, method, path, body)
    assert r.status_code == 403, f"{who} {method} {path}: {r.status_code} {r.text[:120]}"
    assert snapshot(session) == before


def test_an_unknown_id_and_another_professors_id_look_the_same(client, ids):
    """The point of 404: Karim can't tell Rania's group from one that doesn't exist."""
    other = client.get(f"/groups/{ids['lab']}", headers=auth("karim"))
    unknown = client.get("/groups/00000000-0000-0000-0000-000000000000", headers=auth("karim"))
    assert (other.status_code, other.json()) == (unknown.status_code, unknown.json())


def test_the_department_admin_observes_every_group(client, ids):
    """Dana manages nothing above, but she can read every group's budget (she bills them)."""
    for group in (ids["fyp1"], ids["lab"]):
        assert client.get(f"/groups/{group}", headers=auth("dana")).status_code == 200
        assert client.get(f"/groups/{group}/budget", headers=auth("dana")).status_code == 200


# ---------------------------------------------------------------- lists show only your own things
def _ids(rows, key="id"):
    return {str(r[key]) for r in rows}


@pytest.mark.parametrize("who, groups", [
    ("karim", {"fyp1", "fyp2"}),
    ("rania", {"research_lab"}),
    ("sara", {"fyp1", "research_lab"}),
    ("jad", {"fyp2"}),
])
def test_group_list_is_scoped(client, who, groups):
    assert _ids(client.get("/groups", headers=auth(who)).json()) == {str(seed_id(g)) for g in groups}


@pytest.mark.parametrize("who, pods", [
    ("karim", {"res_omar_pod", "res_lina_pod"}),  # his groups' pods, not Maya's
    ("rania", {"res_maya_pod"}),
    ("omar", {"res_omar_pod"}),  # his own only
    ("sara", set()),  # she has no pod, and doesn't see her teammates'
])
def test_pod_list_is_scoped(client, who, pods):
    assert _ids(client.get("/pods", headers=auth(who)).json()) == {str(seed_id(p)) for p in pods}


@pytest.mark.parametrize("who, keys", [
    ("karim", {"res_sara_fyp1_key"}),
    ("rania", {"res_sara_lab_key"}),
    ("sara", {"res_sara_fyp1_key", "res_sara_lab_key"}),
    ("omar", set()),
])
def test_key_list_is_scoped(client, who, keys):
    assert _ids(client.get("/keys", headers=auth(who)).json()) == {str(seed_id(k)) for k in keys}


def test_professors_see_only_their_own_groups_requests(client):
    karims = client.get("/access-requests", headers=auth("karim")).json()
    assert karims and {r["group_id"] for r in karims} <= {str(seed_id("fyp1")), str(seed_id("fyp2"))}
    ranias = client.get("/access-requests", headers=auth("rania")).json()
    assert ranias and {r["group_id"] for r in ranias} == {str(seed_id("research_lab"))}
    # asking for another professor's group by id gives an empty list, not her requests
    assert client.get(f"/access-requests?group_id={seed_id('research_lab')}", headers=auth("karim")).json() == []


def test_students_see_only_their_own_requests(client):
    mine = client.get("/access-requests", headers=auth("sara")).json()
    assert mine and {r["user_id"] for r in mine} == {str(seed_id("u_sara"))}


def test_a_professor_sees_only_his_own_billing_row(client):
    rows = client.get("/reports/billing?period=2026-11", headers=auth("karim")).json()["rows"]
    assert [r["professor_id"] for r in rows] == [str(seed_id("prof_a"))]


def test_another_professors_escalations_are_not_listed(client, ids):
    assert client.get("/escalations", headers=auth("karim")).json() == []
    assert [e["id"] for e in client.get("/escalations", headers=auth("rania")).json()] == [ids["esc_lab"]]


def test_the_snapshot_does_notice_a_change(client, session, ids):
    """Control: the right professor's top-up changes the snapshot, so 'changes nothing' above means something."""
    _signed_in(client, "karim")
    before = snapshot(session)
    r = _call(client, ids, "karim", "POST", "/groups/{fyp1}/top-ups", {"amount": "5", "note": "demo week"})
    assert r.status_code == 201
    assert snapshot(session) != before
