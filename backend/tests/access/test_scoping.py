"""A2: every query is scoped to the caller's subtree.

The Week 1 "done when": a professor cannot read another professor's groups.
"""

import pytest
from fastapi import HTTPException

from app.core.auth import load_current_user
from app.core.scoping import Scope
from app.models import Group, User
from tests.conftest import auth, seed_id

FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")


def scope_for(session, user_key: str) -> Scope:
    return Scope(load_current_user(session, session.get(User, seed_id(user_key))), session)


def names(rows) -> list[str]:
    return sorted(r["name"] if isinstance(r, dict) else r.name for r in rows)


# ---------------------------------------------------------------- the Week 1 test
def test_professor_cannot_read_another_professors_groups(client):
    # Prof A (Karim) lists only his own groups ...
    r = client.get("/groups", headers=auth("karim"))
    assert r.status_code == 200
    assert names(r.json()) == ["FYP Team 1", "FYP Team 2"]

    # ... and Prof B's group does not exist as far as he can tell.
    assert client.get(f"/groups/{LAB}", headers=auth("karim")).status_code == 404

    # The same in the other direction.
    assert names(client.get("/groups", headers=auth("rania")).json()) == ["Research Lab"]
    assert client.get(f"/groups/{FYP1}", headers=auth("rania")).status_code == 404


# ---------------------------------------------------------------- groups per role
def test_department_admin_sees_every_group(client):
    assert names(client.get("/groups", headers=auth("dana")).json()) == ["FYP Team 1", "FYP Team 2", "Research Lab"]
    assert client.get(f"/groups/{LAB}", headers=auth("dana")).status_code == 200


def test_user_sees_only_their_groups(client):
    assert names(client.get("/groups", headers=auth("sara")).json()) == ["FYP Team 1", "Research Lab"]
    assert names(client.get("/groups", headers=auth("omar")).json()) == ["FYP Team 1"]
    assert client.get(f"/groups/{LAB}", headers=auth("omar")).status_code == 404


def test_group_detail_hides_teammates_from_users_but_not_from_the_professor(client):
    members = client.get(f"/groups/{FYP1}", headers=auth("karim")).json()["members"]
    assert names(members) == ["Omar Itani", "Sara Mansour"]
    members = client.get(f"/groups/{FYP1}", headers=auth("omar")).json()["members"]
    assert names(members) == ["Omar Itani"]


# ---------------------------------------------------------------- other tables
def test_users(session):
    karim = {u.name for u in session.exec(scope_for(session, "u_karim").users()).all()}
    assert karim == {"Prof. Karim Nassar", "Sara Mansour", "Omar Itani", "Lina Saleh", "Jad Fares"}
    assert "Maya Aoun" not in karim

    rania = {u.name for u in session.exec(scope_for(session, "u_rania").users()).all()}
    assert rania == {"Prof. Rania Khoury", "Sara Mansour", "Maya Aoun"}

    assert len(session.exec(scope_for(session, "u_dana").users()).all()) == 8
    assert [u.name for u in session.exec(scope_for(session, "u_omar").users()).all()] == ["Omar Itani"]


def test_ledger_is_split_by_professor(session):
    a = session.exec(scope_for(session, "u_karim").ledger_entries()).all()
    assert a and all(e.group_id in (FYP1, FYP2) or e.scope_id == seed_id("prof_a") for e in a)

    b = session.exec(scope_for(session, "u_rania").ledger_entries()).all()
    assert b and all(e.group_id == LAB for e in b)


def test_shared_student_sees_her_own_rows_under_each_professor(session):
    sara = session.exec(scope_for(session, "u_sara").ledger_entries()).all()
    own = [e for e in sara if e.scope_type == "user"]
    assert {e.scope_id for e in own} == {seed_id("u_sara")}
    assert {e.group_id for e in own} == {FYP1, LAB}

    omar = session.exec(scope_for(session, "u_omar").ledger_entries()).all()
    assert all(e.scope_id != seed_id("u_sara") for e in omar)  # not his teammate's rows


def test_issued_resources_and_requests(session):
    assert {r.user_id for r in session.exec(scope_for(session, "u_sara").issued_resources()).all()} == {seed_id("u_sara")}
    assert {r.group_id for r in session.exec(scope_for(session, "u_rania").issued_resources()).all()} == {LAB}
    assert {r.group_id for r in session.exec(scope_for(session, "u_karim").access_requests()).all()} <= {FYP1, FYP2}


def test_credentials(session):
    labels = lambda key: {c.label for c in session.exec(scope_for(session, key).credentials()).all()}  # noqa: E731
    assert labels("u_karim") == {"CS Department OpenRouter", "CS Department RunPod"}  # granted to him
    assert labels("u_rania") == {"Khoury lab OpenRouter", "CS Department RunPod"}  # own + granted
    assert len(labels("u_dana")) == 3
    assert labels("u_sara") == set()


# ---------------------------------------------------------------- manage vs observe
def test_only_the_owning_professor_can_manage_a_group(session):
    assert scope_for(session, "u_karim").require_manage_group(FYP1).id == FYP1

    with pytest.raises(HTTPException) as e:
        scope_for(session, "u_dana").require_manage_group(FYP1)  # department observes only
    assert e.value.status_code == 403

    with pytest.raises(HTTPException) as e:
        scope_for(session, "u_rania").require_manage_group(FYP1)  # not even visible
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        scope_for(session, "u_sara").require_manage_group(FYP1)
    assert e.value.status_code == 403


def test_require_helpers_return_404_outside_scope(session):
    karim = scope_for(session, "u_karim")
    assert isinstance(karim.require_group(FYP2), Group)
    for check, oid in [
        (karim.require_group, LAB),
        (karim.require_user, seed_id("u_maya")),
        (karim.require_professor, seed_id("prof_b")),
        (karim.require_issued_resource, seed_id("res_maya_pod")),
        (karim.require_credential, seed_id("cred_profb_or")),
    ]:
        with pytest.raises(HTTPException) as e:
            check(oid)
        assert e.value.status_code == 404
