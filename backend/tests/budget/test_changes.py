"""A4 + B10: a professor changes his groups' money. The Week 2 "done when":
over-allocation is refused on the real database, and a top-up raises a capped team.

Seed this month: Karim has $400 and gave $50 + $50 (so $300 left). Rania is tracking only,
with her own OpenRouter, so nothing limits what she gives.
"""

from decimal import Decimal as D

from sqlmodel import select

from app.budget.api import BUDGET_NOTE, current_period
from app.budget.ledger import spend_entry
from app.models import AuditLog, LedgerEntry, LedgerType, Provider, ScopeType
from tests.conftest import auth, seed_id

FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")
SARA, OMAR, LINA = seed_id("u_sara"), seed_id("u_omar"), seed_id("u_lina")


def put(client, url, who, **body):
    return client.put(url, headers=auth(who), json=body)


def post(client, url, who, **body):
    return client.post(url, headers=auth(who), json=body)


def lines(session, group_id):
    return list(session.exec(select(LedgerEntry).where(
        LedgerEntry.group_id == group_id, LedgerEntry.period == current_period(),
    ).order_by(LedgerEntry.created_at)).all())


def audits(session, action):
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def summary(client, who="karim"):
    return client.get("/budget/summary", headers=auth(who)).json()


# ---------------------------------------------------------------- PUT /groups/{id}/budget-policy
def test_raising_a_team_budget_applies_this_month(client, session):
    r = put(client, f"/groups/{FYP2}/budget-policy", "karim", monthly_amount="80", rollover=False)
    assert r.status_code == 200
    assert r.json()["team"]["funded"] == "80.0000"
    assert r.json()["policy"] == {"monthly_amount": "80.0000", "rollover": False, "rollover_cap": None}
    last = lines(session, FYP2)[-1]
    assert last.type == LedgerType.adjustment and last.amount == D("30") and last.note.startswith(BUDGET_NOTE)
    assert summary(client)["unallocated"] == "270.0000"
    assert audits(session, "budget.update")


def test_lowering_a_team_budget_takes_money_back(client):
    r = put(client, f"/groups/{FYP2}/budget-policy", "karim", monthly_amount="45")
    assert r.json()["team"]["funded"] == "45.0000"
    assert summary(client)["unallocated"] == "305.0000"


def test_over_allocation_is_refused_and_nothing_is_written(client, session):
    before = len(lines(session, FYP1))
    r = put(client, f"/groups/{FYP1}/budget-policy", "karim", monthly_amount="500")
    assert r.status_code == 409
    assert r.json() == {"detail": "That's more than you have left to give: $300.00.", "unallocated": "300.0000"}
    assert len(lines(session, FYP1)) == before
    assert summary(client)["allocated"] == "100.0000"


def test_a_change_does_not_eat_a_top_up(client):
    post(client, f"/groups/{FYP2}/top-ups", "karim", amount="20", note="demo week")
    r = put(client, f"/groups/{FYP2}/budget-policy", "karim", monthly_amount="60")
    assert r.json()["team"]["funded"] == "80.0000"  # 60 budget + the 20 top-up


def test_tracking_only_professor_with_his_own_credential_has_no_limit(client):
    r = put(client, f"/groups/{LAB}/budget-policy", "rania", monthly_amount="5000")
    assert r.status_code == 200
    assert r.json()["team"]["funded"] == "5002.0000"  # the new $5000 + the seed's $2 correction


def test_only_the_groups_own_professor_changes_it(client):
    assert put(client, f"/groups/{FYP1}/budget-policy", "dana", monthly_amount="10").status_code == 403
    assert put(client, f"/groups/{FYP1}/budget-policy", "rania", monthly_amount="10").status_code == 404
    assert put(client, f"/groups/{FYP1}/budget-policy", "sara", monthly_amount="10").status_code == 403


def test_money_must_be_a_string(client):
    assert put(client, f"/groups/{FYP1}/budget-policy", "karim", monthly_amount=50.5).status_code == 422


# ---------------------------------------------------------------- members
def test_member_budgets_list_spend_and_limits(client):
    rows = {r["user_id"]: r for r in client.get(f"/groups/{FYP1}/member-budgets", headers=auth("karim")).json()}
    assert rows[str(SARA)] == {"user_id": str(SARA), "spent": "8.5000",
                               "limit": {"funded": "20.0000", "spent": "8.5000", "remaining": "11.5000"}}
    assert rows[str(OMAR)]["limit"]["funded"] == "25.0000"


def test_change_and_remove_a_members_limit(client):
    url = f"/groups/{FYP1}/members/{SARA}/budget-policy"
    assert put(client, url, "karim", monthly_amount="30").json()["limit"]["funded"] == "30.0000"
    assert client.get(f"/groups/{FYP1}/budget", headers=auth("sara")).json()["mine"]["funded"] == "30.0000"
    assert put(client, url, "karim", monthly_amount=None).json()["limit"] is None
    assert client.get(f"/groups/{FYP1}/budget", headers=auth("sara")).json()["mine"] is None


def test_a_members_limit_cannot_pass_the_team(client):
    r = put(client, f"/groups/{FYP1}/members/{OMAR}/budget-policy", "karim", monthly_amount="500")
    assert r.status_code == 409 and "$100.00" in r.json()["detail"]


def test_not_a_member_is_404(client):
    assert put(client, f"/groups/{FYP1}/members/{LINA}/budget-policy", "karim", monthly_amount="5").status_code == 404


# ---------------------------------------------------------------- top-ups and corrections (B10)
def test_top_up_reopens_a_capped_team(client, session):
    """The plan's 'done when': a capped team gets money again after a top-up."""
    session.add(spend_entry(LINA, FYP2, current_period(), D("9"), provider=Provider.runpod,
                            credential_id=seed_id("cred_dept_rp"), resource_id=seed_id("res_lina_pod")))
    session.commit()
    assert client.get(f"/groups/{FYP2}/budget", headers=auth("karim")).json()["team"]["remaining"] == "0.0000"

    r = post(client, f"/groups/{FYP2}/top-ups", "karim", amount="20", note="Demo week")
    assert r.status_code == 201 and r.json()["team"]["remaining"] == "20.0000"
    last = lines(session, FYP2)[-1]
    assert (last.type, last.scope_type, last.note) == (LedgerType.adjustment, ScopeType.group, "Demo week")
    assert summary(client)["allocated"] == "120.0000"  # a top-up counts as money given
    assert any(a.details.get("kind") == "top_up" for a in audits(session, "ledger.adjustment"))


def test_top_up_needs_a_reason_and_a_positive_amount(client):
    url = f"/groups/{FYP2}/top-ups"
    assert post(client, url, "karim", amount="20", note="  ").status_code == 422
    assert post(client, url, "karim", amount="0", note="x").status_code == 422
    assert post(client, url, "karim", amount="-5", note="x").status_code == 422


def test_top_up_cannot_pass_the_ceiling(client):
    assert post(client, f"/groups/{FYP2}/top-ups", "karim", amount="301", note="x").status_code == 409


def test_correction_can_take_money_back(client, session):
    r = post(client, f"/groups/{FYP1}/corrections", "karim", amount="-10", note="typed 50 instead of 40")
    assert r.status_code == 201 and r.json()["team"]["funded"] == "90.0000"
    assert lines(session, FYP1)[-1].note == "Correction: typed 50 instead of 40"


def test_correction_adding_money_respects_the_ceiling(client):
    assert post(client, f"/groups/{FYP1}/corrections", "karim", amount="301", note="x").status_code == 409
    assert post(client, f"/groups/{FYP1}/corrections", "karim", amount="0", note="x").status_code == 422