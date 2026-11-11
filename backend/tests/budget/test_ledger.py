"""B1: ledger arithmetic, allocation checks, rollover, top-ups. Pure functions, no database."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from app.budget.ledger import (
    BudgetStatus,
    Totals,
    allocation_entry,
    available_to_user,
    budget_status,
    key_limit,
    money,
    next_period,
    period_of,
    prev_period,
    professor_totals,
    spend_entry,
    team_totals,
    usage_delta,
    user_totals,
)
from app.budget.policies import OverAllocation, check_allocation, check_children_fit
from app.budget.rollover import plan_rollover, rollover_amount
from app.budget.topups import correction_entry, top_up_entry
from app.models import BudgetPolicy, LedgerType, Provider, ScopeType
from scripts.seed import build, seed_id

G, U, P = ScopeType.group, ScopeType.user, ScopeType.professor
PROF, TEAM, SARA, OMAR = (uuid.uuid4() for _ in range(4))
CRED, RES = uuid.uuid4(), uuid.uuid4()
M1, M2 = "2026-11", "2026-12"


def spend(user, amount, period=M1, group=TEAM):
    return spend_entry(user, group, period, D(amount), provider=Provider.openrouter, credential_id=CRED, resource_id=RES)


def team_policy(monthly="50", cap="100", rollover=True):
    return BudgetPolicy(scope_type=G, scope_id=TEAM, group_id=TEAM, monthly_amount=D(monthly),
                        rollover=rollover, rollover_cap=None if cap is None else D(cap))


def test_periods():
    assert period_of(datetime(2026, 11, 30, tzinfo=timezone.utc)) == "2026-11"
    assert next_period("2026-12") == "2027-01"
    assert prev_period("2027-01") == "2026-12"
    assert next_period("2026-01") == "2026-02" and prev_period("2026-02") == "2026-01"


def test_team_balance_counts_every_members_spend():
    entries = [allocation_entry(G, TEAM, M1, D("50"), group_id=TEAM), spend(SARA, "12.5"), spend(OMAR, "7.5")]
    t = team_totals(entries, TEAM, M1)
    assert (t.funded, t.spent, t.remaining) == (D("50"), D("20"), D("30"))


def test_user_sub_budget_is_inside_the_team_not_extra_money():
    entries = [
        allocation_entry(G, TEAM, M1, D("50"), group_id=TEAM),
        allocation_entry(U, SARA, M1, D("20"), group_id=TEAM),
        spend(SARA, "15"),
        spend(OMAR, "30"),
    ]
    team = team_totals(entries, TEAM, M1)
    assert team.funded == D("50")
    assert team.remaining == D("5")
    sara = user_totals(entries, SARA, TEAM, M1)
    assert sara.remaining == D("5")
    assert available_to_user(team, sara) == D("5")
    assert available_to_user(team, None) == D("5")


def test_balances_are_per_period():
    entries = [allocation_entry(G, TEAM, M1, D("50"), group_id=TEAM), spend(SARA, "50"),
               allocation_entry(G, TEAM, M2, D("50"), group_id=TEAM)]
    assert team_totals(entries, TEAM, M1).remaining == 0
    assert team_totals(entries, TEAM, M2).remaining == D("50")


def test_professor_unallocated():
    entries = [allocation_entry(P, PROF, M1, D("400")),
               allocation_entry(G, TEAM, M1, D("150"), group_id=TEAM), spend(SARA, "10")]
    t = professor_totals(entries, PROF, [TEAM], M1)
    assert (t.funded, t.allocated, t.unallocated, t.spent) == (D("400"), D("150"), D("250"), D("10"))


def test_matches_the_seed_ledger():
    """Cross-check against Person 1's seed: the numbers its docstring promises."""
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    entries = [r for r in build(now) if r.__tablename__ == "ledger_entries"]
    fyp1 = team_totals(entries, seed_id("fyp1"), "2026-11")
    assert fyp1.funded == D("100")
    assert fyp1.spent == D("20.8")
    fyp2 = team_totals(entries, seed_id("fyp2"), "2026-11")
    assert budget_status(fyp2) is BudgetStatus.warning
    lab = team_totals(entries, seed_id("research_lab"), "2026-11")
    assert lab.funded == D("152")
    sara_fyp1 = user_totals(entries, seed_id("u_sara"), seed_id("fyp1"), "2026-11")
    sara_lab = user_totals(entries, seed_id("u_sara"), seed_id("research_lab"), "2026-11")
    assert sara_fyp1.remaining == D("11.5") and sara_lab.remaining == D("24.8")


@pytest.mark.parametrize("spent,status", [
    ("0", BudgetStatus.ok), ("39.99", BudgetStatus.ok), ("40", BudgetStatus.warning),
    ("49.99", BudgetStatus.warning), ("50", BudgetStatus.exhausted), ("52", BudgetStatus.exhausted),
])
def test_warn_at_80_cap_at_100(spent, status):
    assert budget_status(Totals(D("50"), D(spent))) is status


def test_no_money_means_exhausted():
    assert budget_status(Totals(D("0"), D("0"))) is BudgetStatus.exhausted


def test_key_limit_formula():
    assert key_limit(D("8.5"), D("30")) == D("38.5")
    assert key_limit(D("8.5"), D("-2")) == D("8.5")


def test_usage_delta():
    assert usage_delta(None, D("3")) == D("3")
    assert usage_delta(D("3"), D("5.25")) == D("2.25")
    assert usage_delta(D("5"), D("4")) == 0


def test_over_allocation_refused():
    check_allocation(D("400"), D("300"), D("100"))
    with pytest.raises(OverAllocation) as e:
        check_allocation(D("400"), D("300"), D("100.01"))
    assert e.value.available == D("100")


def test_tracking_only_has_no_ceiling():
    check_allocation(None, D("10000"), D("5000"))


def test_children_must_fit_together():
    check_children_fit(D("100"), [D("50"), D("50")])
    with pytest.raises(OverAllocation):
        check_children_fit(D("100"), [D("50"), D("50"), D("1")])


def test_50_unused_in_month_1_is_100_in_month_2():
    """The plan's 'done when' check for B1."""
    m1 = [allocation_entry(G, TEAM, M1, D("50"), group_id=TEAM)]
    [carry] = plan_rollover([team_policy()], m1, M1, {})
    assert (carry.type, carry.period, carry.amount, carry.group_id) == (LedgerType.rollover, M2, D("50"), TEAM)
    m2 = m1 + [carry, allocation_entry(G, TEAM, M2, D("50"), group_id=TEAM)]
    assert team_totals(m2, TEAM, M2).remaining == D("100")


def test_cap_stops_unlimited_accumulation():
    assert rollover_amount(D("100"), D("50"), True, D("100")) == D("50")
    assert rollover_amount(D("100"), D("50"), True, None) == D("100")


def test_no_rollover_and_no_debt():
    assert rollover_amount(D("30"), D("50"), False, None) == 0
    assert rollover_amount(D("-2"), D("50"), True, None) == 0
    assert rollover_amount(D("30"), None, True, None) == 0


def test_partial_spend_rolls_the_rest():
    entries = [allocation_entry(G, TEAM, M1, D("50"), group_id=TEAM), spend(SARA, "35")]
    [carry] = plan_rollover([team_policy()], entries, M1, {})
    assert carry.amount == D("15")


def test_professor_rolls_only_unallocated_money():
    policy = BudgetPolicy(scope_type=P, scope_id=PROF, monthly_amount=D("400"), rollover=True, rollover_cap=D("800"))
    entries = [allocation_entry(P, PROF, M1, D("400")), allocation_entry(G, TEAM, M1, D("100"), group_id=TEAM)]
    [carry] = plan_rollover([policy], entries, M1, {PROF: [TEAM]})
    assert carry.amount == D("300") and carry.group_id is None


def test_20_top_up_reopens_a_capped_team():
    """The plan's 'done when' check: a $20 top-up adds to the balance as an adjustment."""
    entries = [allocation_entry(G, TEAM, M1, D("50"), group_id=TEAM), spend(SARA, "50")]
    assert budget_status(team_totals(entries, TEAM, M1)) is BudgetStatus.exhausted
    entries.append(top_up_entry(G, TEAM, M1, D("20"), group_id=TEAM, created_by=PROF))
    t = team_totals(entries, TEAM, M1)
    assert entries[-1].type is LedgerType.adjustment
    assert t.remaining == D("20") and budget_status(t) is BudgetStatus.ok


def test_correction_can_go_either_way_and_needs_a_reason():
    c = correction_entry(G, TEAM, M1, D("-2"), group_id=TEAM, created_by=None, note="double-counted storage")
    assert c.amount == D("-2") and c.note.startswith("Correction:")
    with pytest.raises(ValueError):
        correction_entry(G, TEAM, M1, D("-2"), group_id=TEAM, created_by=None, note="  ")
    with pytest.raises(ValueError):
        top_up_entry(G, TEAM, M1, D("-5"), group_id=TEAM, created_by=PROF)


def test_entry_builders_guard_signs_and_floats():
    assert spend(SARA, "5").amount == D("-5")
    with pytest.raises(ValueError):
        spend(SARA, "0")
    with pytest.raises(ValueError):
        allocation_entry(G, TEAM, M1, D("-1"), group_id=TEAM)
    with pytest.raises(TypeError):
        money(0.1)