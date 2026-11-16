"""The new-month job: on Dec 1, November's leftovers carry over (decision D2) and every
saved budget gets its monthly amount. Safe to run twice.

Seed November: FYP Team 1 had $100 and spent $20.80 (cap $100); FYP Team 2 had $50 and
spent $41 (cap $100); the Research Lab and the student limits don't roll over. Karim has a
$400 ceiling (cap $800) and gave his teams $100; Rania is tracking only.
"""

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from sqlmodel import select

from app.budget.ledger import team_totals
from app.core.config import get_settings
from app.jobs.rollover import run_new_month
from app.models import AuditLog, LedgerEntry, LedgerType, ScopeType
from tests.conftest import seed_id

DEC_1 = datetime(2026, 12, 1, 0, 5, tzinfo=timezone.utc)
FYP1, FYP2, LAB = seed_id("fyp1"), seed_id("fyp2"), seed_id("research_lab")
PROF_A, PROF_B, SARA = seed_id("prof_a"), seed_id("prof_b"), seed_id("u_sara")
SECRET = "test-job-secret"


@pytest.fixture(autouse=True)
def job_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "job_secret", SECRET)


def december(session, type_=None):
    q = select(LedgerEntry).where(LedgerEntry.period == "2026-12")
    if type_ is not None:
        q = q.where(LedgerEntry.type == type_)
    return list(session.exec(q).all())


def amount(lines, scope_type, scope_id):
    return sum((e.amount for e in lines if e.scope_type == scope_type and e.scope_id == scope_id), D("0"))


def test_leftovers_carry_over_capped_by_d2(session):
    result = run_new_month(session, DEC_1)
    assert result.period == "2026-12"
    carried = december(session, LedgerType.rollover)
    assert amount(carried, ScopeType.group, FYP1) == D("79.2")  # $100 - $20.80, under its $100 cap
    assert amount(carried, ScopeType.group, FYP2) == D("9")
    assert amount(carried, ScopeType.group, LAB) == 0  # rollover off
    assert amount(carried, ScopeType.professor, PROF_A) == D("300")  # what he never gave out
    assert amount(carried, ScopeType.professor, PROF_B) == 0  # tracking only


def test_every_budget_gets_its_monthly_amount(session):
    run_new_month(session, DEC_1)
    given = december(session, LedgerType.allocation)
    assert amount(given, ScopeType.professor, PROF_A) == D("400")
    assert amount(given, ScopeType.group, FYP1) == D("50")
    assert amount(given, ScopeType.group, LAB) == D("150")
    assert sum(e.amount for e in given if e.scope_type == ScopeType.user and e.scope_id == SARA) == D("60")  # 20 + 40
    assert not [e for e in given if e.scope_id == PROF_B]  # tracking only: no ceiling to allocate
    assert all(e.created_by is None for e in given)  # written by the system


def test_a_team_starts_december_with_its_budget_plus_leftovers(session):
    run_new_month(session, DEC_1)
    assert team_totals(december(session), FYP1, "2026-12").remaining == D("129.2")  # 50 + 79.20


def test_running_it_twice_adds_nothing(session):
    first = run_new_month(session, DEC_1)
    assert first.rollover_entries > 0 and first.allocation_entries > 0
    before = len(december(session))
    second = run_new_month(session, DEC_1)
    assert (second.rollover_entries, second.allocation_entries) == (0, 0)
    assert len(december(session)) == before


def test_it_is_audited(session):
    run_new_month(session, DEC_1)
    [entry] = session.exec(select(AuditLog).where(AuditLog.action == "budget.new_month")).all()
    assert entry.actor_id is None and entry.target_id == "2026-12"


def test_endpoint_needs_the_job_secret(client):
    assert client.post("/jobs/rollover").status_code == 401
    assert client.post("/jobs/rollover", headers={"X-Job-Secret": "wrong"}).status_code == 401


def test_endpoint_starts_the_current_month(client):
    r = client.post("/jobs/rollover", headers={"X-Job-Secret": SECRET})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"period", "rollover_entries", "allocation_entries", "skipped"}
    assert body["skipped"] is False