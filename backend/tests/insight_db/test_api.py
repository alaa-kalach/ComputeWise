"""A6 + A7 through the HTTP API: escalations, notifications, reports, and the real actions
behind an approval (stop a pod, disable a key, move budget). Seed data, mock providers."""
import re
from decimal import Decimal

import pytest
from sqlmodel import select

import app.adapters as adapters_pkg
from app.access import pods as pods_mod
from app.access.pods import available_budget
from app.adapters.mock import WORLD
from app.insight.wiring import escalation_service
from app.models import AccessRequest, AuditLog, IssuedResource, Notification, Provider, RequestStatus, ResourceStatus
from tests.conftest import auth, seed_id

PROF_A, PROF_B, DEPT = str(seed_id("prof_a")), str(seed_id("prof_b")), str(seed_id("dept"))
FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")
OMAR = seed_id("u_omar")
MONEY = re.compile(r"^-?\d+\.\d{4}$")


@pytest.fixture(autouse=True)
def mock_providers(monkeypatch):
    monkeypatch.setattr(adapters_pkg, "adapter_mode", lambda: "mock")
    WORLD.reset()
    pods_mod._gpu_cache.clear()
    yield
    WORLD.reset()


def propose(session, kind, target, professor=PROF_A, department=None, team=FYP1, payload=None):
    e, _ = escalation_service(session).create(
        kind, target, "ISSUE: x\nEVIDENCE: y\nPROPOSED ACTION: z\nIF IGNORED: w",
        professor, department_id=department, team_id=str(team) if team else None, payload=payload)
    return e


def decide(client, e, who, action="approve", **json):
    return client.post(f"/escalations/{e.id}/{action}", headers=auth(who), json=json or None)


def audits(session, action):
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


# ---------------------------------------------------------------- escalations: who sees what
def test_each_recipient_sees_only_his_escalations(client, session):
    mine = propose(session, "propose_pod_stop", "pod-1")
    other = propose(session, "propose_pod_stop", "pod-2", professor=PROF_B, team=LAB)
    dept = propose(session, "escalate_to_department", PROF_A, department=DEPT, team=None)

    ids = lambda who: {x["id"] for x in client.get("/escalations", headers=auth(who)).json()}
    assert ids("karim") == {mine.id}
    assert ids("rania") == {other.id}
    assert ids("dana") == {dept.id}
    assert ids("omar") == set()
    row = client.get("/escalations", headers=auth("karim")).json()[0]
    assert row["status"] == "pending" and row["group_id"] == str(FYP1) and row["created_at"].endswith(("Z", "+00:00"))


def test_status_filter(client, session):
    e = propose(session, "propose_pod_stop", "pod-1")
    assert client.get("/escalations?status=approved", headers=auth("karim")).json() == []
    assert len(client.get("/escalations?status=pending", headers=auth("karim")).json()) == 1
    assert client.get("/escalations?status=nope", headers=auth("karim")).status_code == 422
    assert e.id


# ---------------------------------------------------------------- deciding
def test_reject_with_a_note_changes_nothing_and_is_audited(client, session):
    e = propose(session, "propose_pod_stop", "pod-1")
    r = decide(client, e, "karim", "reject", note="not now")
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["note"] == "not now"
    assert [a for a in audits(session, "escalation.reject") if a.target_id == e.id]


def test_department_acknowledges_an_escalation_to_it(client, session):
    e = propose(session, "escalate_to_department", PROF_A, department=DEPT, team=None)
    r = decide(client, e, "dana")
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["result"] == {"acknowledged": True}


@pytest.mark.parametrize("who, code", [("rania", 403), ("omar", 403), ("dana", 403)])
def test_only_the_recipient_decides(client, session, who, code):
    e = propose(session, "propose_pod_stop", "pod-1")
    assert decide(client, e, who).status_code == code


def test_unknown_and_already_decided(client, session):
    e = propose(session, "escalate_to_department", PROF_A, department=DEPT, team=None)
    assert client.post(f"/escalations/{seed_id('nothing')}/approve", headers=auth("dana")).status_code == 404
    assert decide(client, e, "dana").status_code == 200
    assert decide(client, e, "dana").status_code == 409


# ---------------------------------------------------------------- the real actions
def launch_pod(client, who="omar"):
    r = client.post("/pods", headers=auth(who), json={"group_id": str(FYP1), "gpu_type_id": "RTX A4000", "cloud": "community"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_approving_a_pod_stop_stops_the_pod(client, session):
    pod_id = launch_pod(client)
    e = propose(session, "propose_pod_stop", pod_id)
    r = decide(client, e, "karim")
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert session.get(IssuedResource, pod_id).status == ResourceStatus.stopped
    assert [a for a in audits(session, "pod.stop") if a.target_id == pod_id]


def test_approving_a_key_disable_disables_the_key(client, session):
    req = session.exec(select(AccessRequest).where(
        AccessRequest.user_id == OMAR, AccessRequest.group_id == FYP1, AccessRequest.provider == Provider.openrouter)).one()
    req.status = RequestStatus.approved
    session.add(req)
    session.flush()
    key_id = client.post("/keys", headers=auth("omar"), json={"group_id": str(FYP1)}).json()["resource_id"]
    e = propose(session, "propose_key_disable", key_id)
    r = decide(client, e, "karim")
    assert r.json()["status"] == "approved", r.text
    assert session.get(IssuedResource, key_id).status == ResourceStatus.disabled


def test_approving_a_budget_move_moves_the_money(client, session):
    a0, b0 = available_budget(session, FYP1, None), available_budget(session, FYP2, None)
    e = propose(session, "propose_budget_change", "fyp1->fyp2",
                payload={"from_team_id": str(FYP1), "to_team_id": str(FYP2), "amount_usd": 1})
    r = decide(client, e, "karim")
    assert r.json()["status"] == "approved", r.text
    assert available_budget(session, FYP1, None) == a0 - Decimal(1)
    assert available_budget(session, FYP2, None) == b0 + Decimal(1)


def test_an_action_that_cannot_be_done_fails_cleanly(client, session):
    # too much money: the escalation becomes `failed`, with the reason, and nothing moves
    a0 = available_budget(session, FYP1, None)
    e = propose(session, "propose_budget_change", "fyp1->fyp2",
                payload={"from_team_id": str(FYP1), "to_team_id": str(FYP2), "amount_usd": 99999})
    r = decide(client, e, "karim")
    assert r.status_code == 200 and r.json()["status"] == "failed" and "left" in r.json()["result"]["error"]
    assert available_budget(session, FYP1, None) == a0


def test_a_professor_cannot_act_on_another_professors_team(client, session):
    maya_pod = str(seed_id("res_maya_pod"))  # in Research Lab (Prof B)
    e = propose(session, "propose_pod_stop", maya_pod)  # addressed to Karim, but not his team
    r = decide(client, e, "karim")
    assert r.json()["status"] == "failed" and "do not manage" in r.json()["result"]["error"]
    assert session.get(IssuedResource, maya_pod).status == ResourceStatus.running


# ---------------------------------------------------------------- notifications
def test_notifications_are_scoped_and_can_be_marked_read(client, session):
    n = Notification(group_id=FYP1, message="FYP Team 1 spent 80%. Omar is the top spender.", level="warning")
    session.add(n)
    session.flush()
    for who in ("omar", "karim"):
        got = client.get("/notifications", headers=auth(who)).json()
        assert [x["id"] for x in got] == [str(n.id)] and got[0]["read"] is False
    assert client.get("/notifications", headers=auth("rania")).json() == []
    assert client.post(f"/notifications/{n.id}/read", headers=auth("rania")).status_code == 404
    r = client.post(f"/notifications/{n.id}/read", headers=auth("omar"))
    assert r.status_code == 200 and r.json()["read"] is True
    assert client.get("/notifications?unread_only=true", headers=auth("omar")).json() == []


# ---------------------------------------------------------------- reports
def test_weekly_report_by_role(client):
    week = "2026-11-02"
    dept = client.get(f"/reports/weekly?week_start={week}", headers=auth("dana")).json()
    prof = client.get(f"/reports/weekly?week_start={week}", headers=auth("karim")).json()
    assert dept["week_start"] == week and {r["professor_id"] for r in dept["rows"]} == {PROF_A, PROF_B}
    assert [r["professor_id"] for r in prof["rows"]] == [PROF_A]
    for r in dept["rows"]:
        assert all(MONEY.match(r[k]) for k in ("this_week_usd", "last_week_usd", "billable_usd", "observed_usd"))
        assert all(set(g) == {"group_id", "this_week_usd", "last_week_usd", "change_pct"} for g in r["groups"])
    assert set(dept["escalations"]) == {"pending", "approved", "rejected", "failed"}
    assert client.get("/reports/weekly", headers=auth("omar")).status_code == 403


def test_billing_report_json_and_csv(client):
    dept = client.get("/reports/billing?period=2026-11", headers=auth("dana")).json()
    assert dept["period"] == "2026-11" and dept["totals"]["professor_id"] == "TOTAL"
    assert all(MONEY.match(dept["totals"][k]) for k in ("billable", "observed", "unassigned", "total", "openrouter", "runpod"))
    assert sum(Decimal(r["total"]) for r in dept["rows"]) == Decimal(dept["totals"]["total"])
    mine = client.get("/reports/billing?period=2026-11", headers=auth("karim")).json()
    assert [r["professor_id"] for r in mine["rows"]] == [PROF_A]

    csv = client.get("/reports/billing?period=2026-11&format=csv", headers=auth("dana"))
    assert csv.headers["content-type"].startswith("text/csv") and "attachment" in csv.headers["content-disposition"]
    assert csv.text.splitlines()[0].startswith("professor_id,professor_name,period")
    assert client.get("/reports/billing?period=2026-11", headers=auth("omar")).status_code == 403
    assert client.get("/reports/billing?period=Nov", headers=auth("dana")).status_code == 422


def test_trend_is_for_the_department_only(client):
    rows = client.get("/reports/trend", headers=auth("dana")).json()
    assert rows and all(MONEY.match(r["total"]) for r in rows)
    assert [r["period"] for r in rows] == sorted(r["period"] for r in rows)
    assert client.get("/reports/trend", headers=auth("karim")).status_code == 403
