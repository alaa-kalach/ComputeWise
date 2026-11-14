"""B4: pods through ComputeWise, on the mock RunPod.

The Week 2 "done when": a pod launched from the API is attributed to the right group and
returns connection details; an approved student launches his own pod; a student without
approved RunPod access is refused; the department cannot stop a pod.
"""

from decimal import Decimal

import pytest
from sqlmodel import select

import app.adapters as adapters_pkg
from app.access import pods as pods_mod
from app.access.pods import available_budget
from app.adapters.mock import WORLD
from app.models import (
    AuditLog,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    MemberStatus,
    ProfessorProviderBinding,
    Provider,
    ResourceStatus,
    ScopeType,
)
from app.budget.ledger import period_of
from app.models.base import utcnow
from tests.conftest import auth, seed_id

FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")
OMAR, LINA = seed_id("u_omar"), seed_id("u_lina")
DEPT_RP = seed_id("cred_dept_rp")
A4000 = {"group_id": str(FYP1), "gpu_type_id": "RTX A4000", "cloud": "community"}


@pytest.fixture(autouse=True)
def mock_runpod(monkeypatch):
    """Always the mock provider, a clean mock world and an empty GPU cache."""
    monkeypatch.setattr(adapters_pkg, "adapter_mode", lambda: "mock")
    WORLD.reset()
    pods_mod._gpu_cache.clear()
    yield
    WORLD.reset()


def audits(session, action: str) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def launch(client, who="omar", **over):
    return client.post("/pods", headers=auth(who), json={**A4000, **over})


def spend_down_to(session, group_id, user_id, left: str):
    """Write a spend entry so exactly `left` USD remains for this user in this group."""
    gap = available_budget(session, group_id, user_id) - Decimal(left)
    session.add(LedgerEntry(
        scope_type=ScopeType.user, scope_id=user_id, group_id=group_id, period=period_of(utcnow()),
        type=LedgerType.spend, amount=-gap, provider=Provider.runpod, credential_id=DEPT_RP,
    ))
    session.flush()


# ---------------------------------------------------------------- GPU catalog
def test_gpu_list_for_the_launch_form(client):
    r = client.get(f"/runpod/gpus?group_id={FYP1}", headers=auth("omar"))
    assert r.status_code == 200
    gpus = r.json()
    a4000 = [g for g in gpus if g["gpu_type_id"] == "RTX A4000"]
    assert {g["cloud"] for g in a4000} == {"community", "secure"}
    assert a4000[0]["price_per_hour"] == "0.1700" and a4000[0]["availability"] == "high"
    assert float(a4000[0]["affordable_hours"]) > 1
    assert next(g for g in gpus if g["gpu_type_id"] == "H100 PCIe")["availability"] == "none"
    assert [Decimal(g["price_per_hour"]) for g in gpus] == sorted(Decimal(g["price_per_hour"]) for g in gpus)


def test_gpu_list_scope(client, session):
    assert client.get(f"/runpod/gpus?group_id={FYP1}", headers=auth("rania")).status_code == 404
    assert client.get(f"/runpod/gpus?group_id={FYP1}", headers=auth("dana")).status_code == 200
    session.delete(session.exec(select(ProfessorProviderBinding).where(
        ProfessorProviderBinding.professor_id == seed_id("prof_a"),
        ProfessorProviderBinding.provider == Provider.runpod)).one())
    session.flush()
    assert client.get(f"/runpod/gpus?group_id={FYP1}", headers=auth("omar")).status_code == 409


# ---------------------------------------------------------------- launch
def test_approved_student_launches_his_own_pod(client, session):
    r = launch(client)
    assert r.status_code == 201, r.text
    pod = r.json()
    assert pod["state"] == "starting" and pod["group_id"] == str(FYP1) and pod["user_id"] == str(OMAR)
    assert pod["gpu_name"] == "RTX A4000" and pod["price_per_hour"] == "0.1700"
    assert pod["storage_per_hour"] == "0.0027" and pod["volume_gb"] == 10

    res = session.get(IssuedResource, pod["id"])
    assert res.credential_id == DEPT_RP and res.status == ResourceStatus.running
    assert WORLD.resources[res.external_ref].name == f"cw-{str(FYP1)[:8]}-{str(OMAR)[:8]}"  # D17
    (entry,) = [a for a in audits(session, "pod.launch") if a.target_id == pod["id"]]
    assert entry.details["billable"] is True and entry.details["gpu"] == "RTX A4000"


@pytest.mark.parametrize(
    "who, body, code",
    [
        ("sara", {}, 403),  # in FYP Team 1, but no approved RunPod access
        ("karim", {}, 403),  # the professor isn't a member: students launch their own
        ("dana", {}, 403),
        ("maya", {}, 404),  # not her group
        ("omar", {"gpu_type_id": "Nope 9000"}, 422),
        ("omar", {"gpu_count": 2}, 422),
        ("omar", {"volume_gb": 5}, 422),
    ],
)
def test_launch_refusals(client, who, body, code):
    assert launch(client, who, **body).status_code == code
    assert WORLD.resources == {}


def test_suspended_student_cannot_launch(client, session):
    m = session.exec(select(GroupMember).where(GroupMember.group_id == FYP1, GroupMember.user_id == OMAR)).one()
    m.status = MemberStatus.suspended
    session.add(m)
    session.flush()
    assert launch(client).status_code == 403


def test_unavailable_gpu_is_refused(client):
    r = launch(client, gpu_type_id="H100 PCIe")
    assert r.status_code == 409 and r.json()["reason"] == "unavailable"


def test_budget_must_cover_one_hour(client, session):
    spend_down_to(session, FYP1, OMAR, "0.05")
    r = launch(client)
    assert r.status_code == 409
    assert r.json() == {
        "detail": r.json()["detail"], "reason": "budget",
        "needed_usd": "0.1727", "remaining_usd": "0.0500",  # $0.17 GPU + 20 GB storage for 1 hour
    }
    assert WORLD.resources == {}


def test_provider_down_is_503(client):
    WORLD.outage = True
    assert launch(client).status_code == 503


# ---------------------------------------------------------------- connection details
def test_connection_details_and_jupyter_password_for_the_owner_only(client, session):
    pod = launch(client).json()
    r = client.get(f"/pods/{pod['id']}", headers=auth("omar"))
    assert r.status_code == 200
    d = r.json()
    assert d["state"] == "running" and d["connection"]["ssh_proxy"].startswith("ssh ")
    assert d["connection"]["jupyter_url"].endswith("-8888.proxy.runpod.net")
    assert d["connection"]["jupyter_password"] == "mock-jupyter-password"
    assert session.get(IssuedResource, pod["id"]).details["ready"] is True

    prof = client.get(f"/pods/{pod['id']}", headers=auth("karim")).json()
    assert prof["connection"]["ssh_proxy"] and prof["connection"]["jupyter_password"] is None
    assert client.get(f"/pods/{pod['id']}", headers=auth("dana")).json()["connection"]["jupyter_password"] is None
    # The list now says running too (readiness was recorded).
    assert [p["state"] for p in client.get("/pods", headers=auth("omar")).json() if p["id"] == pod["id"]] == ["running"]


@pytest.mark.parametrize("who", ["sara", "rania", "lina"])  # teammate, other professor, other team
def test_others_cannot_see_the_pod(client, who):
    pod = launch(client).json()
    assert client.get(f"/pods/{pod['id']}", headers=auth(who)).status_code == 404


def test_detail_when_runpod_is_down_shows_stored_state(client):
    pod = launch(client).json()
    WORLD.outage = True
    d = client.get(f"/pods/{pod['id']}", headers=auth("omar")).json()
    assert d["state"] == "starting" and d["connection"] is None


# ---------------------------------------------------------------- stop / start / terminate
def test_owner_stops_and_starts(client, session):
    pod = launch(client).json()
    ref = session.get(IssuedResource, pod["id"]).external_ref

    r = client.post(f"/pods/{pod['id']}/stop", headers=auth("omar"))
    assert r.status_code == 200 and r.json()["state"] == "stopped" and r.json()["stopped_by"] == "owner"
    assert WORLD.resources[ref].state.value == "disabled"  # volume kept, GPU released
    assert client.post(f"/pods/{pod['id']}/stop", headers=auth("omar")).status_code == 409

    r = client.post(f"/pods/{pod['id']}/start", headers=auth("omar"))
    assert r.status_code == 200 and r.json()["state"] == "starting" and r.json()["stopped_by"] is None
    r = client.post(f"/pods/{pod['id']}/start", headers=auth("omar"))
    assert r.status_code == 409 and r.json()["reason"] == "not_stopped"
    assert len(audits(session, "pod.start")) == 1


def test_professor_stops_a_students_pod(client):
    pod = launch(client).json()
    r = client.post(f"/pods/{pod['id']}/stop", headers=auth("karim"))
    assert r.status_code == 200 and r.json()["stopped_by"] == "professor"


@pytest.mark.parametrize("who, code", [("dana", 403), ("rania", 404), ("sara", 404)])
@pytest.mark.parametrize("action", ["stop", "start"])
def test_only_owner_or_professor_controls_a_pod(client, who, code, action):
    pod = launch(client).json()
    if action == "start":
        client.post(f"/pods/{pod['id']}/stop", headers=auth("omar"))
    assert client.post(f"/pods/{pod['id']}/{action}", headers=auth(who)).status_code == code


def test_restart_has_the_same_budget_check(client, session):
    pod = launch(client).json()
    client.post(f"/pods/{pod['id']}/stop", headers=auth("omar"))
    spend_down_to(session, FYP1, OMAR, "0.10")
    r = client.post(f"/pods/{pod['id']}/start", headers=auth("omar"))
    assert r.status_code == 409 and r.json()["reason"] == "budget"


def test_suspended_owner_cannot_restart_but_professor_can(client, session):
    pod = launch(client).json()
    client.post(f"/pods/{pod['id']}/stop", headers=auth("omar"))
    m = session.exec(select(GroupMember).where(GroupMember.group_id == FYP1, GroupMember.user_id == OMAR)).one()
    m.status = MemberStatus.suspended
    session.add(m)
    session.flush()
    assert client.post(f"/pods/{pod['id']}/start", headers=auth("omar")).status_code == 403
    assert client.post(f"/pods/{pod['id']}/start", headers=auth("karim")).status_code == 200


def test_terminate(client, session):
    pod = launch(client).json()
    ref = session.get(IssuedResource, pod["id"]).external_ref
    assert client.delete(f"/pods/{pod['id']}", headers=auth("dana")).status_code == 403
    assert client.delete(f"/pods/{pod['id']}", headers=auth("omar")).status_code == 204
    assert WORLD.resources[ref].state.value == "gone"
    assert session.get(IssuedResource, pod["id"]).status == ResourceStatus.terminated
    assert client.delete(f"/pods/{pod['id']}", headers=auth("omar")).status_code == 204  # idempotent
    assert len(audits(session, "pod.terminate")) == 1

    ids = lambda q="": [p["id"] for p in client.get(f"/pods{q}", headers=auth("omar")).json()]  # noqa: E731
    assert pod["id"] not in ids() and pod["id"] in ids("?include_terminated=true")


def test_pod_that_vanished_on_runpod_is_recorded_as_terminated(client, session):
    # The seed pod isn't in the mock world, like a pod RunPod forgot.
    r = client.post(f"/pods/{seed_id('res_omar_pod')}/stop", headers=auth("omar"))
    assert r.status_code == 409
    assert session.get(IssuedResource, seed_id("res_omar_pod")).status == ResourceStatus.terminated
    assert len(audits(session, "pod.gone")) == 1


# ---------------------------------------------------------------- listing
def test_listing_is_scoped(client):
    launch(client)
    users = lambda who: sorted(p["user_name"] for p in client.get("/pods", headers=auth(who)).json())  # noqa: E731
    assert users("omar") == ["Omar Itani", "Omar Itani"]  # the seed pod and the new one
    assert users("karim") == ["Lina Saleh", "Omar Itani", "Omar Itani"]
    assert users("rania") == ["Maya Aoun"]
    assert users("dana") == ["Lina Saleh", "Maya Aoun", "Omar Itani", "Omar Itani"]
    assert users("sara") == []
    assert len(client.get(f"/pods?group_id={FYP2}", headers=auth("karim")).json()) == 1


def test_seed_pod_shows_its_spend_this_month(client):
    pods = {p["id"]: p for p in client.get("/pods", headers=auth("omar")).json()}
    assert pods[str(seed_id("res_omar_pod"))]["spent_this_month_usd"] == "12.3000"
