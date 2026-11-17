"""A6 audit and A7 reports on the real schema and the seed data."""
from datetime import date

import pytest
from sqlmodel import select

from app.insight import models as m
from app.insight.wiring import escalation_service, report_service
from app.models import AuditLog
from tests.conftest import seed_id

PROF_A, DEPT = str(seed_id("prof_a")), str(seed_id("dept"))
KARIM = str(seed_id("u_karim"))


def audit_rows(session, esc_id):
    rows = session.exec(select(AuditLog).where(AuditLog.target_type == "escalation",
                                               AuditLog.target_id == str(esc_id)).order_by(AuditLog.at)).all()
    return [(r.action, str(r.actor_id) if r.actor_id else None, r.details) for r in rows]


def karim():
    return m.Actor(user_id=KARIM, role=m.ROLE_PROFESSOR, professor_id=PROF_A)


# ---------------------------------------------------------------- A6
def test_create_and_approve_are_written_to_the_audit_log(session):
    svc = escalation_service(session, {"propose_pod_stop": lambda e: {"stopped": e.target}})
    e, _ = svc.create("propose_pod_stop", "pod-1", "ISSUE: x", PROF_A, team_id=str(seed_id("fyp1")))
    svc.approve(e.id, karim())
    rows = audit_rows(session, e.id)
    assert [r[0] for r in rows] == ["escalation.create", "escalation.approve"]
    assert rows[0][1] is None  # created by the agent: no user
    assert rows[1][1] == KARIM  # approved by the professor
    assert rows[1][2]["target"] == "pod-1"


def test_reject_and_failure_are_audited(session):
    def boom(e):
        raise RuntimeError("provider down")
    svc = escalation_service(session, {"propose_key_disable": boom})
    a, _ = svc.create("propose_key_disable", "key-1", "ISSUE: x", PROF_A)
    svc.approve(a.id, karim())
    b, _ = svc.create("propose_pod_stop", "pod-9", "ISSUE: y", PROF_A)
    svc.reject(b.id, karim(), note="not now")
    assert [r[0] for r in audit_rows(session, a.id)] == ["escalation.create", "escalation.fail"]
    assert [r[0] for r in audit_rows(session, b.id)] == ["escalation.create", "escalation.reject"]


def test_a_refused_decision_writes_no_decision_entry(session):
    import uuid
    svc = escalation_service(session)
    e, _ = svc.create("propose_pod_stop", "pod-2", "ISSUE: z", PROF_A)
    stranger = m.Actor(user_id=str(uuid.uuid4()), role=m.ROLE_PROFESSOR, professor_id=str(uuid.uuid4()))
    with pytest.raises(Exception):
        svc.approve(e.id, stranger)
    assert [r[0] for r in audit_rows(session, e.id)] == ["escalation.create"]


# ---------------------------------------------------------------- A7
DEPT_ACTOR = m.Actor(user_id=str(seed_id("u_dana")), role=m.ROLE_DEPARTMENT_ADMIN, department_id=DEPT)


def test_billing_report_comes_from_the_real_ledger(session):
    report = report_service(session).billing_report(DEPT_ACTOR, "2026-11")
    by = {r["professor_name"]: r for r in report["rows"]}
    karim_row, rania_row = by["Prof. Karim Nassar"], by["Prof. Rania Khoury"]
    # Karim: Sara 8.50 + Omar 12.30 + Lina 41.00, all on the department credential
    assert (karim_row["billable"], karim_row["observed"], karim_row["total"]) == (61.8, 0.0, 61.8)
    # Rania: Maya 22.00 on the department credential, Sara 15.20 on Rania's own
    assert (rania_row["billable"], rania_row["observed"]) == (22.0, 15.2)
    assert report["totals"]["billable"] == 83.8 and report["totals"]["observed"] == 15.2
    assert karim_row["openrouter"] == 8.5 and karim_row["runpod"] == 53.3


def test_a_professor_sees_only_his_own_row(session):
    report = report_service(session).billing_report(karim(), "2026-11")
    assert [r["professor_id"] for r in report["rows"]] == [PROF_A]


def test_a_student_gets_no_report(session):
    student = m.Actor(user_id=str(seed_id("u_sara")), role="user")
    with pytest.raises(Exception):
        report_service(session).billing_report(student, "2026-11")


def test_weekly_report_from_the_ledger(session):
    # the seed's spend is on Nov 10, so the week of Monday Nov 9 holds it
    report = report_service(session).weekly_report(DEPT_ACTOR, date(2026, 11, 9))
    row = next(r for r in report["rows"] if r["professor_id"] == PROF_A)
    assert (row["this_week_usd"], row["billable_usd"], row["observed_usd"]) == (61.8, 61.8, 0.0)
    assert {t["team_id"] for t in row["teams"]} == {str(seed_id("fyp1")), str(seed_id("fyp2"))}
    assert report["totals"]["this_week_usd"] == 99.0
