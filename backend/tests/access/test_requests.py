"""A5: access requests. The Week 2 "done when": a student request shows up for the
professor, and only the group's own professor approves or rejects it (D21)."""

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.access.requests import require_approved_access
from app.models import (
    AccessRequest,
    AuditLog,
    GroupMember,
    MemberStatus,
    ProfessorProviderBinding,
    Provider,
    RequestStatus,
)
from tests.conftest import auth, seed_id

FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")
SARA, OMAR, LINA, JAD = seed_id("u_sara"), seed_id("u_omar"), seed_id("u_lina"), seed_id("u_jad")


def audits(session, action: str) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def jads_request(session) -> AccessRequest:
    return session.exec(select(AccessRequest).where(AccessRequest.user_id == JAD)).one()


def suspend(session, group_id, user_id):
    m = session.exec(select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)).one()
    m.status = MemberStatus.suspended
    session.add(m)
    session.flush()


# ---------------------------------------------------------------- the Week 2 flow
def test_student_request_shows_up_for_the_professor_and_he_approves_it(client, session):
    r = client.post("/access-requests", headers=auth("lina"),
                    json={"group_id": str(FYP2), "provider": "openrouter", "justification": "Captioning demo"})
    assert r.status_code == 201
    req = r.json()
    assert req["status"] == "pending" and req["user_name"] == "Lina Saleh" and req["group_name"] == "FYP Team 2"

    pending = client.get("/access-requests?status=pending", headers=auth("karim")).json()
    assert req["id"] in [p["id"] for p in pending]

    r = client.post(f"/access-requests/{req['id']}/approve", headers=auth("karim"))
    assert r.status_code == 200
    assert r.json()["status"] == "approved" and r.json()["decided_by_name"] == "Prof. Karim Nassar"
    (entry,) = [a for a in audits(session, "access_request.approve") if a.target_id == req["id"]]
    assert entry.details["credential_id"] == str(seed_id("cred_dept_or"))
    require_approved_access(session, LINA, FYP2, Provider.openrouter)  # no exception


# ---------------------------------------------------------------- listing
def test_listing_is_scoped(client):
    ids = lambda who, q="": {(r["user_name"], r["provider"]) for r in client.get(f"/access-requests{q}", headers=auth(who)).json()}  # noqa: E731
    assert ids("jad") == {("Jad Fares", "openrouter")}
    assert len(client.get("/access-requests", headers=auth("karim")).json()) == 5
    assert ids("rania") == {("Sara Mansour", "openrouter"), ("Maya Aoun", "runpod")}
    assert len(client.get("/access-requests", headers=auth("dana")).json()) == 7
    assert ids("karim", "?status=pending") == {("Jad Fares", "openrouter")}
    assert len(client.get(f"/access-requests?group_id={FYP1}", headers=auth("karim")).json()) == 3
    # Sara sees her own two requests, one under each professor, and nothing of Omar's.
    assert ids("sara") == {("Sara Mansour", "openrouter")} and len(client.get("/access-requests", headers=auth("sara")).json()) == 2


def test_listing_is_newest_first(client):
    rows = client.get("/access-requests", headers=auth("dana")).json()
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)


# ---------------------------------------------------------------- creating
def test_duplicate_requests_are_refused(client):
    assert client.post("/access-requests", headers=auth("jad"),
                       json={"group_id": str(FYP2), "provider": "openrouter"}).status_code == 409  # pending
    assert client.post("/access-requests", headers=auth("omar"),
                       json={"group_id": str(FYP1), "provider": "runpod"}).status_code == 409  # approved


def test_rejected_request_can_be_asked_again(client):
    r = client.post("/access-requests", headers=auth("omar"), json={"group_id": str(FYP1), "provider": "openrouter"})
    assert r.status_code == 201


def test_request_only_in_your_own_groups(client):
    r = client.post("/access-requests", headers=auth("omar"), json={"group_id": str(LAB), "provider": "runpod"})
    assert r.status_code == 404


def test_suspended_member_cannot_request(client, session):
    suspend(session, FYP2, LINA)
    r = client.post("/access-requests", headers=auth("lina"), json={"group_id": str(FYP2), "provider": "openrouter"})
    assert r.status_code == 409


def test_create_is_audited(client, session):
    client.post("/access-requests", headers=auth("lina"), json={"group_id": str(FYP2), "provider": "openrouter"})
    assert len(audits(session, "access_request.create")) == 1


# ---------------------------------------------------------------- deciding
@pytest.mark.parametrize(
    "who, code",
    [
        ("dana", 403),  # the department observes, never decides
        ("rania", 404),  # another professor's group is invisible
        ("jad", 403),  # the student himself
        ("lina", 404),  # a teammate can't even see it
    ],
)
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_only_the_groups_professor_decides(client, session, who, code, action):
    req = jads_request(session)
    assert client.post(f"/access-requests/{req.id}/{action}", headers=auth(who)).status_code == code
    session.refresh(req)
    assert req.status == RequestStatus.pending


def test_decided_request_cannot_be_decided_again(client, session):
    req = jads_request(session)
    assert client.post(f"/access-requests/{req.id}/approve", headers=auth("karim")).status_code == 200
    assert client.post(f"/access-requests/{req.id}/approve", headers=auth("karim")).status_code == 409
    assert client.post(f"/access-requests/{req.id}/reject", headers=auth("karim")).status_code == 409


def test_approve_needs_a_usable_credential(client, session):
    b = session.exec(select(ProfessorProviderBinding).where(
        ProfessorProviderBinding.professor_id == seed_id("prof_a"),
        ProfessorProviderBinding.provider == Provider.openrouter)).one()
    session.delete(b)
    session.flush()
    r = client.post(f"/access-requests/{jads_request(session).id}/approve", headers=auth("karim"))
    assert r.status_code == 409 and "credential" in r.json()["detail"]


def test_approve_refused_if_student_was_suspended_meanwhile(client, session):
    suspend(session, FYP2, JAD)
    assert client.post(f"/access-requests/{jads_request(session).id}/approve", headers=auth("karim")).status_code == 409


def test_reject_with_reason_is_shown_to_the_student(client, session):
    req = jads_request(session)
    r = client.post(f"/access-requests/{req.id}/reject", headers=auth("karim"),
                    json={"reason": "  Use the team key for the demo  "})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    mine = client.get("/access-requests", headers=auth("jad")).json()
    assert mine[0]["reason"] == "Use the team key for the demo"
    assert audits(session, "access_request.reject")[0].details["reason"] == "Use the team key for the demo"


def test_reject_without_a_body(client, session):
    r = client.post(f"/access-requests/{jads_request(session).id}/reject", headers=auth("karim"))
    assert r.status_code == 200 and r.json()["reason"] is None


# ---------------------------------------------------------------- require_approved_access (for B3, B4)
def test_require_approved_access(session):
    require_approved_access(session, OMAR, FYP1, Provider.runpod)  # approved in the seed
    for user, group, provider in [
        (OMAR, FYP1, Provider.openrouter),  # rejected
        (SARA, FYP1, Provider.runpod),  # never asked
        (JAD, FYP2, Provider.openrouter),  # pending
        (OMAR, LAB, Provider.runpod),  # not a member
    ]:
        with pytest.raises(HTTPException) as exc:
            require_approved_access(session, user, group, provider)
        assert exc.value.status_code == 403


def test_suspension_removes_approved_access(session):
    suspend(session, FYP1, OMAR)
    with pytest.raises(HTTPException):
        require_approved_access(session, OMAR, FYP1, Provider.runpod)
