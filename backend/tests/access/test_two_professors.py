"""A8: a user in groups under two professors has a separate budget under each (design: Roles and
hierarchy; data model: group_id on budget policies and ledger entries).

Seed: Sara is in FYP Team 1 (Prof. Nassar, her own limit $20, spent $8.50) and in the Research Lab
(Prof. Khoury, her own limit $40, spent $15.20). Checked through the API she and her professors use.
"""

from decimal import Decimal

from app.models import LedgerEntry, LedgerType, Provider, ScopeType
from tests.conftest import auth, seed_id

FYP1, LAB, SARA = seed_id("fyp1"), seed_id("research_lab"), seed_id("u_sara")


def budget(client, group, who="sara"):
    r = client.get(f"/groups/{group}/budget", headers=auth(who))
    assert r.status_code == 200, r.text
    return r.json()


def member(client, group, who):
    rows = client.get(f"/groups/{group}/member-budgets", headers=auth(who)).json()
    return next(r for r in rows if r["user_id"] == str(SARA))


def spend(session, group, amount, period):
    session.add(LedgerEntry(
        scope_type=ScopeType.user, scope_id=SARA, group_id=group, period=period, type=LedgerType.spend,
        amount=-Decimal(amount), provider=Provider.openrouter,
        credential_id=seed_id("cred_dept_or" if group == FYP1 else "cred_profb_or"),
        resource_id=seed_id("res_sara_fyp1_key" if group == FYP1 else "res_sara_lab_key"),
    ))
    session.flush()


def test_she_sees_one_budget_per_professor(client):
    fyp, lab = budget(client, FYP1), budget(client, LAB)
    assert (fyp["mine"]["funded"], fyp["mine"]["spent"], fyp["available"]) == ("20.0000", "8.5000", "11.5000")
    assert (lab["mine"]["funded"], lab["mine"]["spent"], lab["available"]) == ("40.0000", "15.2000", "24.8000")
    assert fyp["team"] != lab["team"]


def test_spending_under_one_professor_leaves_the_other_untouched(client, session):
    lab_before = budget(client, LAB)
    fyp_before = budget(client, FYP1)
    spend(session, FYP1, "5", fyp_before["period"])

    fyp = budget(client, FYP1)
    assert fyp["mine"]["spent"] == "13.5000" and fyp["available"] == "6.5000"
    assert fyp["team"]["spent"] == str(Decimal(fyp_before["team"]["spent"]) + 5)
    assert budget(client, LAB) == lab_before  # not a cent moved in the Lab


def test_using_up_one_budget_does_not_block_the_other(client, session):
    spend(session, FYP1, "11.5", budget(client, FYP1)["period"])
    assert budget(client, FYP1)["available"] == "0.0000"
    assert budget(client, LAB)["available"] == "24.8000"


def test_each_professor_sees_only_her_spend_in_his_own_group(client):
    assert member(client, FYP1, "karim")["spent"] == "8.5000"
    assert member(client, LAB, "rania")["spent"] == "15.2000"


def test_a_limit_set_by_one_professor_does_not_change_the_other(client):
    r = client.put(f"/groups/{FYP1}/members/{SARA}/budget-policy", headers=auth("karim"),
                   json={"monthly_amount": "30"})
    assert r.status_code == 200, r.text
    assert budget(client, FYP1)["mine"]["funded"] == "30.0000"
    assert budget(client, LAB)["mine"]["funded"] == "40.0000"
    # and he cannot reach her Lab limit at all (404, not his group)
    r = client.put(f"/groups/{LAB}/members/{SARA}/budget-policy", headers=auth("karim"), json={"monthly_amount": "1"})
    assert r.status_code == 404
