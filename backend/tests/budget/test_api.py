"""A4: the budget endpoints, on the real database with the seed.

Seed this month: FYP Team 1 = $50 + $50 rollover, spent $8.50 (Sara's key) + $12.30 (Omar's
pod). FYP Team 2 = $50, spent $41. Research Lab = $150 + a $2 correction, spent $37.20.
Karim has a $400 ceiling; Rania is tracking only.
"""

from tests.conftest import auth, seed_id

FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")


def get(client, url, who):
    return client.get(url, headers=auth(who))


# ---------------------------------------------------------------- GET /groups/{id}/budget
def test_professor_sees_his_team(client):
    r = get(client, f"/groups/{FYP1}/budget", "karim")
    assert r.status_code == 200
    b = r.json()
    assert b["team"] == {"funded": "100.0000", "spent": "20.8000", "remaining": "79.2000"}
    assert b["rolled_over"] == "50.0000"
    assert b["by_provider"] == {"openrouter": "8.5000", "runpod": "12.3000"}
    assert b["policy"] == {"monthly_amount": "50.0000", "rollover": True, "rollover_cap": "100.0000"}
    assert b["mine"] is None  # he is not a member
    assert b["funding"]["billable"] is True  # Karim uses department credentials


def test_student_sees_his_own_limit_inside_the_team(client):
    b = get(client, f"/groups/{FYP1}/budget", "sara").json()
    assert b["mine"] == {"funded": "20.0000", "spent": "8.5000", "remaining": "11.5000"}
    assert b["available"] == "11.5000"  # min(team 79.20, hers 11.50)


def test_student_without_a_limit_is_limited_by_the_team(client):
    b = get(client, f"/groups/{FYP2}/budget", "lina").json()
    assert b["mine"] is None and b["available"] == "9.0000"


def test_mixed_funding_is_neither_billable_nor_observed(client):
    # Rania: her own OpenRouter (observed) + the department's RunPod (billable).
    assert get(client, f"/groups/{LAB}/budget", "rania").json()["funding"]["billable"] is None


def test_outside_your_scope_is_404(client):
    assert get(client, f"/groups/{FYP1}/budget", "rania").status_code == 404  # another professor
    assert get(client, f"/groups/{FYP2}/budget", "sara").status_code == 404  # not her team


# ---------------------------------------------------------------- GET /budget/summary
def test_summary_with_a_ceiling(client):
    s = get(client, "/budget/summary", "karim").json()
    assert (s["ceiling"], s["allocated"], s["unallocated"], s["spent"]) == (
        "400.0000", "100.0000", "300.0000", "61.8000",
    )


def test_summary_tracking_only(client):
    s = get(client, "/budget/summary", "rania").json()
    assert s["ceiling"] is None and s["unallocated"] is None
    assert s["allocated"] == "152.0000"  # $150 + the $2 correction


def test_summary_is_for_professors_only(client):
    assert get(client, "/budget/summary", "sara").status_code == 403
    assert get(client, "/budget/summary", "dana").status_code == 403  # the department observes