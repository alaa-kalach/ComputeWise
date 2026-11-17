import pytest

from app.agent.tools.forecast import forecast_spend


def test_steady_spend_stays_within_budget():
    r = forecast_spend([1.0] * 10, days_in_period=30, balance_usd=50)
    assert r["spent_usd"] == 10.0
    assert r["daily_rate_usd"] == 1.0
    assert r["projected_total_usd"] == 30.0
    assert r["will_exceed"] is False
    assert r["days_until_runout"] is None


def test_fast_spend_predicts_runout_day():
    # $3/day for 10 days = $30 spent, $20 left, $3/day -> 6.7 more days
    r = forecast_spend([3.0] * 10, days_in_period=30, balance_usd=50)
    assert r["will_exceed"] is True
    assert r["projected_total_usd"] == 90.0
    assert r["days_until_runout"] == 6.7


def test_uses_recent_window_not_whole_history():
    series = [0.0] * 10 + [4.0] * 7
    r = forecast_spend(series, days_in_period=30, balance_usd=100, window_days=7)
    assert r["daily_rate_usd"] == 4.0


def test_already_over_budget_runs_out_now():
    r = forecast_spend([6.0] * 10, days_in_period=30, balance_usd=50)
    assert r["days_until_runout"] == 0.0
    assert r["remaining_usd"] == 0.0


def test_no_spend_yet():
    r = forecast_spend([], days_in_period=30, balance_usd=50)
    assert r["projected_total_usd"] == 0.0
    assert r["will_exceed"] is False


def test_rollover_balance_counts():
    # $100 balance (50 + 50 rollover) makes the same spend safe
    r = forecast_spend([3.0] * 10, days_in_period=30, balance_usd=100)
    assert r["will_exceed"] is False


def test_bad_input_is_rejected():
    with pytest.raises(ValueError):
        forecast_spend([1.0], days_in_period=0, balance_usd=50)
    with pytest.raises(ValueError):
        forecast_spend([-1.0], days_in_period=30, balance_usd=50)
