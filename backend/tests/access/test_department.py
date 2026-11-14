"""A10: the department adds and removes professors and sets each one's ceiling or tracking
only (D1). The Week 2 "done when": the department sets a professor's ceiling or tracking
only, and only the department can."""

from decimal import Decimal

import pytest
from sqlmodel import select

from app.access import department as dept_mod
from app.access.credentials import CredentialRevoked, on_credential_revoked
from app.access import credentials as creds_mod
from app.budget.ledger import period_of
from app.models import (
    AuditLog,
    BudgetPolicy,
    CredentialGrant,
    GrantStatus,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    MemberStatus,
    Professor,
    ProfessorProviderBinding,
    ResourceStatus,
    ScopeType,
)
from app.models.base import utcnow
from tests.conftest import auth, make_token, seed_id

PROF_A, PROF_B, LAB = seed_id("prof_a"), seed_id("prof_b"), seed_id("research_lab")
NEW = "new.prof@cs.example.edu"


@pytest.fixture(autouse=True)
def invites(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(dept_mod, "send_invite", lambda email: sent.append(email) or True)
    return sent


def audits(session, action):
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def prof_entries(session, professor_id):
    return list(session.exec(select(LedgerEntry).where(
        LedgerEntry.scope_type == ScopeType.professor, LedgerEntry.scope_id == professor_id,
        LedgerEntry.period == period_of(utcnow()),
    ).order_by(LedgerEntry.created_at)).all())


def put(client, prof, who="dana", **body):
    return client.put(f"/professors/{prof}/budget-setting", headers=auth(who), json=body)


def as_new_prof():
    return {"Authorization": f"Bearer {make_token(NEW)}"}


# ---------------------------------------------------------------- listing
def test_department_sees_its_professors_with_their_setting(client):
    r = client.get("/professors", headers=auth("dana"))
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    karim, rania = by_name["Prof. Karim Nassar"], by_name["Prof. Rania Khoury"]
    assert karim["budget_setting"] == {"mode": "ceiling", "monthly_amount": "400.0000", "rollover": True,
                                       "rollover_cap": "800.0000"}
    assert karim["group_count"] == 2
    assert rania["budget_setting"]["mode"] == "tracking" and rania["budget_setting"]["monthly_amount"] is None
    assert rania["group_count"] == 1


def test_professor_sees_only_himself_and_students_nothing(client):
    assert [p["name"] for p in client.get("/professors", headers=auth("karim")).json()] == ["Prof. Karim Nassar"]
    assert client.get("/professors", headers=auth("sara")).json() == []


# ---------------------------------------------------------------- adding
def test_add_new_professor(client, session, invites):
    r = client.post("/professors", headers=auth("dana"), json={"email": " New.Prof@cs.example.edu ", "name": "Prof. New"})
    assert r.status_code == 201
    out = r.json()
    assert out["invited"] is True and invites == [NEW]
    assert out["budget_setting"]["mode"] == "tracking" and out["group_count"] == 0
    assert out["department_id"] == str(seed_id("dept"))
    # On first login he gets the professor dashboard.
    me = client.get("/me", headers=as_new_prof()).json()
    assert me["role"] == "professor" and me["professor_id"] == out["id"]
    assert len(audits(session, "professor.add")) == 1

    assert client.post("/professors", headers=auth("dana"), json={"email": NEW, "name": "x"}).status_code == 409


def test_existing_user_becomes_professor_without_invite(client, invites):
    r = client.post("/professors", headers=auth("dana"), json={"email": "jad.fares@students.example.edu", "name": "Jad"})
    assert r.status_code == 201 and r.json()["invited"] is False and invites == []
    assert r.json()["name"] == "Jad Fares"  # keeps his existing name


@pytest.mark.parametrize("who", ["karim", "sara"])
def test_only_the_department_adds_professors(client, who):
    assert client.post("/professors", headers=auth(who), json={"email": NEW, "name": "x"}).status_code == 403


@pytest.mark.parametrize("body", [{"email": "nope", "name": "x"}, {"email": NEW, "name": " "}, {"email": NEW}])
def test_add_validation(client, body):
    assert client.post("/professors", headers=auth("dana"), json=body).status_code == 422


# ---------------------------------------------------------------- budget setting
def test_tracking_professor_gets_a_ceiling_effective_this_month(client, session):
    r = put(client, PROF_B, mode="ceiling", monthly_amount="300", rollover=False)
    assert r.status_code == 200, r.text
    assert r.json()["budget_setting"] == {"mode": "ceiling", "monthly_amount": "300.0000", "rollover": False,
                                          "rollover_cap": None}
    (entry,) = prof_entries(session, PROF_B)
    assert (entry.type, entry.amount) == (LedgerType.allocation, Decimal("300"))
    (a,) = audits(session, "budget.update")
    assert a.details["old"]["mode"] == "tracking" and a.details["new"]["monthly_amount"] == "300.0000"


def test_changing_a_ceiling_writes_the_difference_never_edits(client, session):
    assert put(client, PROF_A, mode="ceiling", monthly_amount="500", rollover_cap="800").status_code == 200
    assert put(client, PROF_A, mode="ceiling", monthly_amount="300", rollover_cap="800").status_code == 200
    amounts = [(e.type, e.amount) for e in prof_entries(session, PROF_A)]
    assert amounts == [
        (LedgerType.allocation, Decimal("400")),  # the seed's monthly allocation, untouched
        (LedgerType.adjustment, Decimal("100")),
        (LedgerType.adjustment, Decimal("-200")),
    ]
    assert sum(a for _, a in amounts) == Decimal("300")
    # The same setting again changes nothing.
    put(client, PROF_A, mode="ceiling", monthly_amount="300", rollover_cap="800")
    assert len(prof_entries(session, PROF_A)) == 3 and len(audits(session, "budget.update")) == 2


def test_ceiling_cannot_go_below_what_he_already_gave_his_groups(client, session):
    # Karim allocated $50 + $50 to his two groups this month.
    r = put(client, PROF_A, mode="ceiling", monthly_amount="99.99")
    assert r.status_code == 422 and "$100" in r.json()["detail"]
    assert put(client, PROF_A, mode="ceiling", monthly_amount="100").status_code == 200
    # Rania (tracking) already gave Research Lab $150.
    assert put(client, PROF_B, mode="ceiling", monthly_amount="100").status_code == 422
    assert prof_entries(session, PROF_B) == []


def test_switch_to_tracking_only(client, session):
    r = put(client, PROF_A, mode="tracking")
    assert r.status_code == 200
    assert r.json()["budget_setting"]["mode"] == "tracking"
    assert len(prof_entries(session, PROF_A)) == 1  # nothing written to the ledger
    policy = session.exec(select(BudgetPolicy).where(BudgetPolicy.scope_type == ScopeType.professor,
                                                     BudgetPolicy.scope_id == PROF_A)).one()
    assert policy.monthly_amount is None


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "ceiling"},  # amount missing
        {"mode": "tracking", "monthly_amount": "10"},
        {"mode": "ceiling", "monthly_amount": "-5"},
        {"mode": "ceiling", "monthly_amount": 12.5},  # money is a string (D7)
        {"mode": "monthly"},
    ],
)
def test_budget_setting_validation(client, body):
    assert put(client, PROF_A, **body).status_code == 422


@pytest.mark.parametrize("who, code", [("karim", 403), ("sara", 403), ("rania", 404)])
def test_only_the_department_sets_ceilings(client, who, code):
    """Karim can't raise his own ceiling; a student can't; another professor can't even see it."""
    assert put(client, PROF_A, who=who, mode="ceiling", monthly_amount="9999").status_code == code


# ---------------------------------------------------------------- removing
def test_professor_with_active_members_cannot_be_removed(client):
    assert client.delete(f"/professors/{PROF_A}", headers=auth("dana")).status_code == 409
    assert client.delete(f"/professors/{PROF_B}", headers=auth("dana")).status_code == 409


def test_remove_professor_without_activity_and_add_him_back(client, session):
    prof_id = client.post("/professors", headers=auth("dana"), json={"email": NEW, "name": "Prof. New"}).json()["id"]
    assert client.post("/groups", headers=as_new_prof(), json={"name": "Empty lab"}).status_code == 201

    assert client.delete(f"/professors/{prof_id}", headers=auth("dana")).status_code == 204
    assert session.get(Professor, prof_id).removed_at is not None
    assert NEW not in [p["email"] for p in client.get("/professors", headers=auth("dana")).json()]
    assert client.get("/me", headers=as_new_prof()).json()["role"] == "user"  # lost the role
    assert client.delete(f"/professors/{prof_id}", headers=auth("dana")).status_code == 404
    assert put(client, prof_id, mode="tracking").status_code == 404

    r = client.post("/professors", headers=auth("dana"), json={"email": NEW, "name": "Prof. New"})
    assert r.status_code == 201 and r.json()["id"] == prof_id and r.json()["group_count"] == 1
    assert audits(session, "professor.add")[-1].details["restored"] is True


def test_removal_waits_for_live_resources_then_revokes_his_grants(client, session):
    calls: list[CredentialRevoked] = []
    hook = on_credential_revoked(lambda s, actor, event: calls.append(event))
    try:
        for m in session.exec(select(GroupMember).where(GroupMember.group_id == LAB)).all():
            m.status = MemberStatus.suspended
            session.add(m)
        session.flush()
        # Members suspended, but Maya's pod and Sara's key in the lab are still live.
        assert client.delete(f"/professors/{PROF_B}", headers=auth("dana")).status_code == 409

        for r in session.exec(select(IssuedResource).where(IssuedResource.group_id == LAB)).all():
            r.status = ResourceStatus.terminated if r.provider.value == "runpod" else ResourceStatus.disabled
            session.add(r)
        session.flush()
        assert client.delete(f"/professors/{PROF_B}", headers=auth("dana")).status_code == 204

        grant = session.exec(select(CredentialGrant).where(CredentialGrant.professor_id == PROF_B)).one()
        assert grant.status == GrantStatus.revoked
        assert session.exec(select(ProfessorProviderBinding).where(
            ProfessorProviderBinding.professor_id == PROF_B)).all() == []
        assert calls == [CredentialRevoked(seed_id("cred_dept_rp"), grant_provider(session, grant), PROF_B, "grant.revoke")]
        # Her history stays: the department still sees her group's ledger.
        assert client.get(f"/groups/{LAB}", headers=auth("dana")).status_code == 200
    finally:
        creds_mod._REVOKE_HOOKS.remove(hook)


def grant_provider(session, grant):
    from app.models import ProviderCredential

    return session.get(ProviderCredential, grant.credential_id).provider


@pytest.mark.parametrize("who, code", [("karim", 403), ("rania", 404), ("sara", 403)])
def test_only_the_department_removes(client, who, code):
    assert client.delete(f"/professors/{PROF_A}", headers=auth(who)).status_code == code
