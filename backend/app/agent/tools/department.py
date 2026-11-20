"""Department tools (Person 3): get_professor_budget and escalate_to_department.

    get_professor_budget(professor_id)          read: one professor's ceiling, spend and demand
    escalate_to_department(professor_id, reason) action: ask the department admin to decide

escalate_to_department changes nothing. It creates a PENDING escalation addressed to the
department through EscalationService (24 h cooldown per issue, audit entry escalation.create).
The department admin sees it on the dashboard and decides.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.agent.gates import reason_missing
from app.agent.tools._data import daily_spend
from app.agent.tools.forecast import forecast_spend
from app.budget.ledger import period_of, professor_totals
from app.insight.wiring import escalation_service
from app.models import (
    BudgetPolicy, Group, LedgerEntry, LedgerType, OwnerType, Professor, ProfessorProviderBinding,
    ProviderCredential, ScopeType, User,
)


def _failed(detail: str) -> dict:
    return {"status": "failed", "detail": detail}


def _professor(session: Session, professor_id) -> Professor | None:
    try:
        pid = uuid.UUID(str(professor_id))
    except ValueError:
        return None
    prof = session.get(Professor, pid)
    return prof if prof is not None and prof.removed_at is None else None


def _credential_kind(session: Session, professor_id: uuid.UUID) -> str:
    """"department": the department pays and bills him. "own": he pays from his own account."""
    owners = {
        session.get(ProviderCredential, b.credential_id).owner_type
        for b in session.exec(
            select(ProfessorProviderBinding).where(ProfessorProviderBinding.professor_id == professor_id)
        ).all()
    }
    if OwnerType.department in owners:
        return "department"
    return "own" if owners else "department"


def tool_get_professor_budget(session: Session, professor_id: str, now: datetime | None = None) -> dict:
    """Agent tool: one professor's ceiling, what his teams were given and spent, and demand."""
    prof = _professor(session, professor_id)
    if prof is None:
        return _failed(f"unknown professor: {professor_id}")
    now = now or datetime.now(timezone.utc)
    period = period_of(now)
    days_in_period = calendar.monthrange(now.year, now.month)[1]

    groups = list(session.exec(select(Group).where(Group.professor_id == prof.id)).all())
    group_ids = [g.id for g in groups]
    entries = list(session.exec(
        select(LedgerEntry).where(
            LedgerEntry.period == period,
            (LedgerEntry.group_id.in_(group_ids)) | (LedgerEntry.scope_id == prof.id),  # type: ignore[attr-defined]
        )
    ).all())
    totals = professor_totals(entries, prof.id, group_ids, period)

    policy = session.exec(
        select(BudgetPolicy).where(BudgetPolicy.scope_type == ScopeType.professor,
                                   BudgetPolicy.scope_id == prof.id)
    ).first()
    tracking_only = policy is not None and policy.monthly_amount is None
    ceiling = None if tracking_only else round(float(totals.funded), 2)

    # Demand: each team's projected spend if its recent rate continues (not capped at its balance).
    projected = 0.0
    by_team = daily_spend(entries, lambda e: e.group_id, now)
    for g in groups:
        series = by_team.get(g.id, [0.0] * now.day)
        projected += forecast_spend(series, days_in_period, 0.0)["projected_total_usd"]

    spent = round(float(totals.spent), 2)
    billable = observed = 0.0
    owner_of: dict = {}
    for e in entries:
        if e.type != LedgerType.spend or e.group_id not in group_ids:
            continue
        if e.credential_id not in owner_of:
            cred = session.get(ProviderCredential, e.credential_id)
            owner_of[e.credential_id] = cred.owner_type if cred else None
        if owner_of[e.credential_id] == OwnerType.department:
            billable += float(-e.amount)   # the department paid: it bills the professor
        else:
            observed += float(-e.amount)   # the professor paid from his own account
    return {
        "status": "ok",
        "professor_id": str(prof.id),
        "name": _name(session, prof),
        "grant_ceiling_usd": ceiling,
        "tracking_only": tracking_only,
        "credential": _credential_kind(session, prof.id),
        "allocated_usd": round(float(totals.allocated), 2),
        "spent_usd": spent,
        "billable_spent_usd": round(billable, 2),
        "observed_spent_usd": round(observed, 2),
        "pct_of_ceiling_spent": round(spent / ceiling, 3) if ceiling else None,
        "projected_demand_usd": round(projected, 2),
        "will_exceed_ceiling": bool(ceiling is not None and projected > ceiling),
        "team_ids": [str(g.id) for g in groups],
    }


def _name(session: Session, prof: Professor) -> str:
    user = session.get(User, prof.user_id)
    return user.name if user else str(prof.id)


def tool_escalate_to_department(session: Session, professor_id: str, reason: str) -> dict:
    """Agent tool: ask the department admin to decide about one professor."""
    missing = reason_missing(reason)
    if missing:
        return _failed("reason is incomplete. Write it as four lines, each starting with its label: "
                       f"ISSUE:, EVIDENCE:, PROPOSED ACTION:, IF IGNORED:. Missing: {', '.join(missing)}.")
    prof = _professor(session, professor_id)
    if prof is None:
        return _failed(f"unknown professor: {professor_id}")

    svc = escalation_service(session)
    e, created = svc.create(kind="escalate_to_department", target=str(prof.id), reason=reason,
                            professor_id=str(prof.id), department_id=str(prof.department_id))
    session.commit()  # the store saved the escalation; this saves its audit entry
    if not created:
        return {"status": "ok", "created": False, "escalation_id": e.id,
                "detail": "already escalated: the same issue is pending or was raised in the last "
                          "24 hours. Do not escalate it again."}
    return {"status": "ok", "created": True, "escalation_id": e.id,
            "detail": "escalation sent to the department"}
