"""A5: groups and members. Only the group's own professor changes a group (D21); suspending
a member stops his access in THAT group only; every change is audited."""

import pytest
from sqlmodel import select

from app.access import groups as groups_mod
from app.adapters import ProviderUnavailable, ResourceNotFound
from app.models import AuditLog, Group, GroupMember, IssuedResource, MemberStatus, ResourceStatus, User
from tests.conftest import auth, make_token, seed_id

FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")
SARA, OMAR, JAD, MAYA = seed_id("u_sara"), seed_id("u_omar"), seed_id("u_jad"), seed_id("u_maya")


def audits(session, action: str) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


class FakeAdapter:
    """Records stop/resume calls; can be told to fail."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def stop(self, ref):
        self.calls.append(("stop", ref))
        if self.fail:
            raise self.fail

    def resume(self, ref):
        self.calls.append(("resume", ref))
        if self.fail:
            raise self.fail


@pytest.fixture
def provider(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setattr(groups_mod, "adapter_for_credential", lambda session, credential_id: fake)
    return fake


@pytest.fixture
def invites(monkeypatch):
    sent: list[str] = []

    def fake_send(email):
        sent.append(email)
        return True

    monkeypatch.setattr(groups_mod, "send_invite", fake_send)
    return sent


# ---------------------------------------------------------------- groups
def test_professor_creates_a_group(client, session):
    r = client.post("/groups", headers=auth("karim"), json={"name": "  FYP Team 3 "})
    assert r.status_code == 201
    assert r.json()["name"] == "FYP Team 3" and r.json()["professor_id"] == str(seed_id("prof_a"))
    assert len(audits(session, "group.create")) == 1
    assert "FYP Team 3" in [g["name"] for g in client.get("/groups", headers=auth("karim")).json()]


def test_group_names_are_unique_per_professor(client):
    assert client.post("/groups", headers=auth("karim"), json={"name": "fyp team 1"}).status_code == 409
    # Another professor may use the same name.
    assert client.post("/groups", headers=auth("rania"), json={"name": "FYP Team 1"}).status_code == 201


@pytest.mark.parametrize("who", ["dana", "sara"])
def test_only_professors_create_groups(client, who):
    assert client.post("/groups", headers=auth(who), json={"name": "X"}).status_code == 403


def test_rename(client, session):
    r = client.patch(f"/groups/{FYP2}", headers=auth("karim"), json={"name": "Vision Team"})
    assert r.status_code == 200 and r.json()["name"] == "Vision Team"
    assert audits(session, "group.rename")[0].details == {"old_name": "FYP Team 2", "name": "Vision Team"}
    assert client.patch(f"/groups/{FYP2}", headers=auth("karim"), json={"name": "FYP Team 1"}).status_code == 409
    assert client.patch(f"/groups/{FYP1}", headers=auth("karim"), json={"name": " "}).status_code == 422


@pytest.mark.parametrize("who, code", [("rania", 404), ("dana", 403), ("sara", 403)])
def test_only_the_groups_professor_renames(client, who, code):
    assert client.patch(f"/groups/{FYP1}", headers=auth(who), json={"name": "Hijacked"}).status_code == code


# ---------------------------------------------------------------- adding members
def test_add_existing_user_from_another_professors_group(client, session, invites):
    r = client.post(f"/groups/{FYP2}/members", headers=auth("karim"), json={"email": "Maya.Aoun@students.example.edu"})
    assert r.status_code == 201
    assert r.json() == {"user_id": str(MAYA), "name": "Maya Aoun", "email": "maya.aoun@students.example.edu",
                        "status": "active", "invited": False}
    assert invites == []  # she already has an account
    assert client.post(f"/groups/{FYP2}/members", headers=auth("karim"),
                       json={"email": "maya.aoun@students.example.edu"}).status_code == 409
    assert audits(session, "member.add")[0].details["new_user"] is False


def test_add_new_user_creates_account_and_invites(client, session, invites):
    r = client.post(f"/groups/{FYP1}/members", headers=auth("karim"), json={"email": "new.student@students.example.edu"})
    assert r.status_code == 422  # name needed for a new email

    r = client.post(f"/groups/{FYP1}/members", headers=auth("karim"),
                    json={"email": "New.Student@students.example.edu", "name": "New Student"})
    assert r.status_code == 201 and r.json()["invited"] is True
    assert invites == ["new.student@students.example.edu"]
    user = session.exec(select(User).where(User.email == "new.student@students.example.edu")).one()
    assert user.name == "New Student" and user.auth_id is None
    # He can log in now: the first login links his Supabase account by email.
    token = make_token("new.student@students.example.edu")
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and [m["group_name"] for m in me.json()["memberships"]] == ["FYP Team 1"]


def test_no_invite_without_service_key(client):
    # The real send_invite with no SUPABASE_SERVICE_ROLE_KEY sends nothing.
    r = client.post(f"/groups/{FYP1}/members", headers=auth("karim"), json={"email": "x@ex.edu", "name": "X"})
    assert r.status_code == 201 and r.json()["invited"] is False


@pytest.mark.parametrize("email", ["not-an-email", "a@b", ""])
def test_bad_email_is_422(client, email):
    r = client.post(f"/groups/{FYP1}/members", headers=auth("karim"), json={"email": email, "name": "X"})
    assert r.status_code == 422


@pytest.mark.parametrize("who, code", [("rania", 404), ("dana", 403), ("omar", 403)])
def test_only_the_groups_professor_adds_members(client, who, code):
    r = client.post(f"/groups/{FYP1}/members", headers=auth(who), json={"email": "x@ex.edu", "name": "X"})
    assert r.status_code == code


# ---------------------------------------------------------------- suspending
def test_suspend_disables_keys_in_this_group_only(client, session, provider):
    r = client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth("karim"), json={"status": "suspended"})
    assert r.status_code == 200 and r.json()["status"] == "suspended"

    fyp1_key = session.get(IssuedResource, seed_id("res_sara_fyp1_key"))
    lab_key = session.get(IssuedResource, seed_id("res_sara_lab_key"))
    assert fyp1_key.status == ResourceStatus.disabled and fyp1_key.details["suspended"] is True
    assert lab_key.status == ResourceStatus.active  # Rania's group: untouched
    assert provider.calls == [("stop", fyp1_key.external_ref)]
    assert [a.target_id for a in audits(session, "key.disable")] == [str(fyp1_key.id)]
    assert audits(session, "member.suspend")[0].details["provider_failed"] == []

    lab = session.exec(select(GroupMember).where(GroupMember.group_id == LAB, GroupMember.user_id == SARA)).one()
    assert lab.status == MemberStatus.active

    # Reactivating re-enables the key the suspension disabled.
    r = client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth("karim"), json={"status": "active"})
    assert r.status_code == 200
    session.refresh(fyp1_key)
    assert fyp1_key.status == ResourceStatus.active and "suspended" not in fyp1_key.details
    assert provider.calls[-1] == ("resume", fyp1_key.external_ref)
    assert len(audits(session, "key.enable")) == 1 and len(audits(session, "member.reactivate")) == 1


def test_suspend_stops_pods_and_reactivating_leaves_them_stopped(client, session, provider):
    client.patch(f"/groups/{FYP1}/members/{OMAR}", headers=auth("karim"), json={"status": "suspended"})
    pod = session.get(IssuedResource, seed_id("res_omar_pod"))
    assert pod.status == ResourceStatus.stopped  # stopped, never terminated (D3)
    assert provider.calls == [("stop", "seedpod-omar-fyp1")]
    assert len(audits(session, "pod.stop")) == 2  # the seed has one already

    client.patch(f"/groups/{FYP1}/members/{OMAR}", headers=auth("karim"), json={"status": "active"})
    session.refresh(pod)
    assert pod.status == ResourceStatus.stopped and "suspended" not in pod.details
    assert provider.calls == [("stop", "seedpod-omar-fyp1")]  # no resume call


def test_provider_down_still_suspends_the_member(client, session, provider):
    provider.fail = ProviderUnavailable("timeout")
    r = client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth("karim"), json={"status": "suspended"})
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    key = session.get(IssuedResource, seed_id("res_sara_fyp1_key"))
    assert key.status == ResourceStatus.active  # unchanged: the provider didn't confirm
    assert audits(session, "member.suspend")[0].details["provider_failed"] == [str(key.id)]


def test_key_already_gone_at_provider(client, session, provider):
    provider.fail = ResourceNotFound("gone")
    client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth("karim"), json={"status": "suspended"})
    assert session.get(IssuedResource, seed_id("res_sara_fyp1_key")).status == ResourceStatus.disabled


def test_suspending_twice_is_a_no_op(client, session, provider):
    client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth("karim"), json={"status": "suspended"})
    client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth("karim"), json={"status": "suspended"})
    assert len(audits(session, "member.suspend")) == 1 and len(provider.calls) == 1


@pytest.mark.parametrize("who, code", [("rania", 404), ("dana", 403), ("omar", 403)])
def test_only_the_groups_professor_suspends(client, who, code, provider):
    r = client.patch(f"/groups/{FYP1}/members/{SARA}", headers=auth(who), json={"status": "suspended"})
    assert r.status_code == code and provider.calls == []


def test_suspend_unknown_member_is_404(client):
    r = client.patch(f"/groups/{FYP1}/members/{MAYA}", headers=auth("karim"), json={"status": "suspended"})
    assert r.status_code == 404


# ---------------------------------------------------------------- removing
def test_remove_member_without_spend(client, session):
    assert client.delete(f"/groups/{FYP2}/members/{JAD}", headers=auth("karim")).status_code == 204
    assert session.exec(select(GroupMember).where(GroupMember.user_id == JAD)).first() is None
    assert len(audits(session, "member.remove")) == 1


@pytest.mark.parametrize("user", [SARA, OMAR])  # Sara spent; Omar spent and has a running pod
def test_member_with_spend_must_be_suspended_instead(client, user):
    assert client.delete(f"/groups/{FYP1}/members/{user}", headers=auth("karim")).status_code == 409


@pytest.mark.parametrize("who, code", [("rania", 404), ("dana", 403)])
def test_only_the_groups_professor_removes(client, who, code):
    assert client.delete(f"/groups/{FYP2}/members/{JAD}", headers=auth(who)).status_code == code


def test_admin_who_is_also_professor_manages_only_his_own_groups(client, session):
    """D21: Dana gets the professor role and her own group. She manages it, and still only
    observes Karim's."""
    from app.models import Professor

    session.add(Professor(id=seed_id("prof_dana"), department_id=seed_id("dept"), user_id=seed_id("u_dana")))
    session.flush()
    r = client.post("/groups", headers=auth("dana"), json={"name": "Dana's Lab"})
    assert r.status_code == 201
    own = r.json()["id"]
    assert client.post(f"/groups/{own}/members", headers=auth("dana"), json={"email": EMAIL_JAD}).status_code == 201
    assert client.patch(f"/groups/{FYP1}", headers=auth("dana"), json={"name": "x"}).status_code == 403
    assert session.get(Group, FYP1).name == "FYP Team 1"


EMAIL_JAD = "jad.fares@students.example.edu"
