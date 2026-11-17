import csv
import io

import pytest

from app.insight import models as m
from app.insight.escalations import PermissionDenied
from app.insight.reports import (ReportService, billing_report_csv, classify,
                                 monthly_billing_report, monthly_totals, spend_by_professor)

CREDS = {"cred-dept-or": "department", "cred-dept-rp": "department", "cred-p2": "professor"}
PROFS = {"prof-1": "Prof. Saab", "prof-2": "Prof. Haddad", "prof-3": "Prof. Khoury"}


def e(period, prof, provider, cred, usd, team="t"):
    return {"period": period, "professor_id": prof, "team_id": team,
            "provider": provider, "credential_id": cred, "usd": usd}


ENTRIES = [
    e("2026-11", "prof-1", "openrouter", "cred-dept-or", 10.0),
    e("2026-11", "prof-1", "runpod", "cred-dept-rp", 5.5),
    e("2026-11", "prof-1", "openrouter", "cred-dept-or", 2.0),
    e("2026-11", "prof-2", "openrouter", "cred-p2", 30.0),      # own credential: observed
    e("2026-11", "prof-2", "runpod", None, 1.0),                 # no credential: unassigned
    e("2026-10", "prof-1", "openrouter", "cred-dept-or", 40.0),  # another month
]
DEPT = m.Actor("u-d", m.ROLE_DEPARTMENT_ADMIN, department_id="dept-1")
PROF1 = m.Actor("u-1", m.ROLE_PROFESSOR, professor_id="prof-1")
PROF2 = m.Actor("u-2", m.ROLE_PROFESSOR, professor_id="prof-2")
STUDENT = m.Actor("u-s", m.ROLE_USER)


def test_classify_billable_observed_unassigned():
    assert classify(ENTRIES[0], CREDS) == "billable"
    assert classify(ENTRIES[3], CREDS) == "observed"
    assert classify(ENTRIES[4], CREDS) == "unassigned"
    assert classify({"credential_id": "unknown"}, CREDS) == "unassigned"


def test_spend_by_professor_splits_billable_observed_and_providers():
    rows = {r["professor_id"]: r for r in spend_by_professor(ENTRIES, "2026-11", CREDS, PROFS)}
    p1, p2 = rows["prof-1"], rows["prof-2"]
    assert (p1["billable"], p1["observed"], p1["total"]) == (17.5, 0.0, 17.5)
    assert (p1["openrouter"], p1["runpod"]) == (12.0, 5.5)
    assert (p2["billable"], p2["observed"], p2["unassigned"], p2["total"]) == (0.0, 30.0, 1.0, 31.0)
    assert p1["professor_name"] == "Prof. Saab"


def test_other_months_are_not_counted_and_idle_professors_appear():
    rows = spend_by_professor(ENTRIES, "2026-11", CREDS, PROFS)
    assert rows[0]["professor_id"] == "prof-2"              # biggest spender first
    idle = [r for r in rows if r["professor_id"] == "prof-3"][0]
    assert idle["total"] == 0.0
    assert [r["total"] for r in spend_by_professor(ENTRIES, "2026-10", CREDS)] == [40.0]


def test_report_totals_add_up():
    report = monthly_billing_report(ENTRIES, "2026-11", CREDS, PROFS)
    t = report["totals"]
    assert (t["billable"], t["observed"], t["unassigned"], t["total"]) == (17.5, 30.0, 1.0, 48.5)
    assert t["billable"] + t["observed"] + t["unassigned"] == t["total"]


def test_cents_do_not_drift():
    many = [e("2026-11", "prof-1", "openrouter", "cred-dept-or", 0.1) for _ in range(30)]
    assert monthly_billing_report(many, "2026-11", CREDS)["totals"]["total"] == 3.0


def test_monthly_totals_trend_is_oldest_first():
    trend = monthly_totals(ENTRIES, CREDS)
    assert [t["period"] for t in trend] == ["2026-10", "2026-11"]
    assert trend[0]["billable"] == 40.0 and trend[1]["total"] == 48.5


def test_csv_has_header_rows_and_total():
    report = monthly_billing_report(ENTRIES, "2026-11", CREDS, PROFS)
    rows = list(csv.DictReader(io.StringIO(billing_report_csv(report))))
    assert rows[0].keys() >= {"professor_id", "period", "billable", "observed", "total"}
    assert rows[-1]["professor_id"] == "TOTAL" and rows[-1]["total"] == "48.5"
    assert {r["period"] for r in rows} == {"2026-11"}


def test_service_scopes_the_report_by_role():
    svc = ReportService(lambda: ENTRIES, CREDS, PROFS)
    assert len(svc.billing_report(DEPT, "2026-11")["rows"]) == 3
    mine = svc.billing_report(PROF1, "2026-11")
    assert [r["professor_id"] for r in mine["rows"]] == ["prof-1"]
    assert mine["totals"]["total"] == 17.5                  # no trace of prof-2's spend
    with pytest.raises(PermissionDenied):
        svc.billing_report(STUDENT, "2026-11")


def test_only_the_department_sees_the_trend():
    svc = ReportService(lambda: ENTRIES, CREDS, PROFS)
    assert len(svc.trend(DEPT)) == 2
    for actor in (PROF1, PROF2, STUDENT):
        with pytest.raises(PermissionDenied):
            svc.trend(actor)


# ---- weekly report ----
from datetime import date, datetime

from app.insight.models import Escalation
from app.insight.reports import weekly_report

W = date(2026, 11, 9)           # week: Nov 9 to Nov 15; the week before: Nov 2 to Nov 8


def d(day, prof, team, cred, usd):
    return {"period": "2026-11", "date": day, "professor_id": prof, "team_id": team,
            "provider": "openrouter", "credential_id": cred, "usd": usd}


WEEK_ENTRIES = [
    d("2026-11-09", "prof-1", "t-a", "cred-dept-or", 10.0),
    d("2026-11-15", "prof-1", "t-a", "cred-dept-or", 5.0),
    d("2026-11-10", "prof-1", "t-b", "cred-dept-or", 5.0),
    d("2026-11-03", "prof-1", "t-a", "cred-dept-or", 10.0),   # the week before
    d("2026-11-12", "prof-2", "t-c", "cred-p2", 8.0),         # own credential: observed
    d("2026-10-20", "prof-1", "t-a", "cred-dept-or", 99.0),   # too old: ignored
    d("2026-11-16", "prof-1", "t-a", "cred-dept-or", 99.0),   # next week: ignored
    e("2026-11", "prof-1", "openrouter", "cred-dept-or", 77.0),  # no date: ignored
]


def esc(day, status):
    return Escalation(id=1, kind="propose_pod_stop", target="x", reason="r",
                      recipient_type="professor", recipient_id="prof-1",
                      created_at=datetime(2026, 11, day), status=status)


def test_weekly_report_compares_with_the_week_before():
    r = weekly_report(WEEK_ENTRIES, W, CREDS, PROFS)
    p1 = next(x for x in r["rows"] if x["professor_id"] == "prof-1")
    assert (p1["this_week_usd"], p1["last_week_usd"], p1["change_pct"]) == (20.0, 10.0, 100.0)
    assert (p1["billable_usd"], p1["observed_usd"]) == (20.0, 0.0)
    team_a = next(t for t in p1["teams"] if t["team_id"] == "t-a")
    assert (team_a["this_week_usd"], team_a["last_week_usd"], team_a["change_pct"]) == (15.0, 10.0, 50.0)
    p2 = next(x for x in r["rows"] if x["professor_id"] == "prof-2")
    assert (p2["observed_usd"], p2["billable_usd"], p2["change_pct"]) == (8.0, 0.0, None)
    assert r["totals"]["this_week_usd"] == 28.0


def test_weekly_report_counts_this_weeks_escalations_by_status():
    r = weekly_report([], W, CREDS, PROFS, [esc(10, "pending"), esc(11, "approved"),
                                           esc(12, "approved"), esc(3, "rejected")])
    assert r["escalations"] == {"pending": 1, "approved": 2, "rejected": 0, "failed": 0}


def test_weekly_report_scopes_by_role():
    svc = ReportService(lambda: WEEK_ENTRIES, CREDS, PROFS)
    assert {x["professor_id"] for x in svc.weekly_report(DEPT, W)["rows"]} == {"prof-1", "prof-2", "prof-3"}
    mine = svc.weekly_report(PROF1, W)
    assert [x["professor_id"] for x in mine["rows"]] == ["prof-1"]
    assert mine["totals"]["this_week_usd"] == 20.0
    with pytest.raises(PermissionDenied):
        svc.weekly_report(STUDENT, W)
