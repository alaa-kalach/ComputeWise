"""propose_* (Person 2, Week 3): the agent asks the team's professor to act.

    propose_pod_stop(pod_id, reason)
    propose_key_disable(key_id, reason)
    propose_budget_change(from_team_id, to_team_id, amount_usd, reason)

Nothing is stopped, disabled or moved here. Each call creates a PENDING escalation through
Person 3's EscalationService (routing to the professor, 24 h cooldown per issue, audit entry
escalation.create). The professor approves it in one click, and only then do the executors in
app/insight/executors.py carry it out, re-checking permissions at that moment.

The targets are the same ids get_usage, list_idle_pods and detect_spend_anomalies return
(issued_resources ids), so the evidence gate can match them.

Checks done here, so a proposal the professor could never approve is not created:
  * the reason has the four lines ISSUE / EVIDENCE / PROPOSED ACTION / IF IGNORED (the
    agent's FormatGuard checks this too; this catches a direct call that skips the gates);
  * the pod or key exists and is still live (a stopped pod can't be stopped again);
  * a budget move is between two different teams of the SAME professor, for a positive
    amount the source team actually has left this period.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlmodel import Session, select

from app.agent.gates import reason_missing
from app.budget.ledger import money, period_of, team_totals
from app.insight.wiring import escalation_service
from app.models import Group, IssuedResource, LedgerEntry, Provider, ResourceStatus


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _failed(detail: str) -> dict:
    return {"status": "failed", "detail": detail}


def _bad_reason(reason) -> dict | None:
    missing = reason_missing(reason)
    if not missing:
        return None
    return _failed("reason is incomplete. Write it as four lines, each starting with its label: "
                   f"ISSUE:, EVIDENCE:, PROPOSED ACTION:, IF IGNORED:. Missing: {', '.join(missing)}.")


def _create(session: Session, kind: str, target: str, reason: str, group: Group,
            payload: dict | None = None) -> dict:
    svc = escalation_service(session)
    e, created = svc.create(kind=kind, target=target, reason=reason,
                            professor_id=str(group.professor_id), team_id=str(group.id),
                            payload=payload)
    session.commit()  # the store saved the escalation; this saves its audit entry
    if not created:
        return {"status": "ok", "created": False, "escalation_id": e.id,
                "detail": "already proposed: the same proposal is pending or was made in the last "
                          "24 hours. Do not propose it again."}
    return {"status": "ok", "created": True, "escalation_id": e.id,
            "detail": "proposal sent to the professor"}


def _resource(session: Session, resource_id: str, provider: Provider) -> IssuedResource | None:
    rid = _uuid(resource_id)
    res = session.get(IssuedResource, rid) if rid else None
    return res if res is not None and res.provider == provider else None


# ---------------------------------------------------------------- pods
def tool_propose_pod_stop(session: Session, pod_id: str, reason: str) -> dict:
    """Agent tool: ask the professor to stop one running pod."""
    if (bad := _bad_reason(reason)):
        return bad
    res = _resource(session, pod_id, Provider.runpod)
    if res is None:
        return _failed(f"unknown pod: {pod_id}")
    if res.status != ResourceStatus.running:
        return _failed(f"the pod is {res.status.value}, not running: there is nothing to stop.")
    group = session.get(Group, res.group_id)
    return _create(session, "propose_pod_stop", str(res.id), reason, group)


# ---------------------------------------------------------------- keys
LIVE_KEY = (ResourceStatus.active, ResourceStatus.capped)


def tool_propose_key_disable(session: Session, key_id: str, reason: str) -> dict:
    """Agent tool: ask the professor to disable one OpenRouter key."""
    if (bad := _bad_reason(reason)):
        return bad
    res = _resource(session, key_id, Provider.openrouter)
    if res is None:
        return _failed(f"unknown key: {key_id}")
    if res.status not in LIVE_KEY:
        return _failed(f"the key is already {res.status.value}: there is nothing to disable.")
    group = session.get(Group, res.group_id)
    return _create(session, "propose_key_disable", str(res.id), reason, group)


# ---------------------------------------------------------------- budget moves
def _amount(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return money(d) if d.is_finite() else None


def tool_propose_budget_change(session: Session, from_team_id: str, to_team_id: str,
                               amount_usd, reason: str, now: datetime | None = None) -> dict:
    """Agent tool: ask the professor to move money from one of his teams to another."""
    if (bad := _bad_reason(reason)):
        return bad
    groups = []
    for t in (from_team_id, to_team_id):
        gid = _uuid(t)
        g = session.get(Group, gid) if gid else None
        if g is None:
            return _failed(f"unknown team: {t}")
        groups.append(g)
    src, dst = groups
    if src.id == dst.id:
        return _failed("the two teams must be different.")
    if src.professor_id != dst.professor_id:
        return _failed("the two teams belong to different professors; budget can only move "
                       "between teams of the same professor.")
    amount = _amount(amount_usd)
    if amount is None or amount <= 0:
        return _failed(f"amount_usd must be a positive number of dollars, not {amount_usd!r}.")

    period = period_of(now or datetime.now(timezone.utc))
    entries = session.exec(
        select(LedgerEntry).where(LedgerEntry.group_id == src.id, LedgerEntry.period == period)
    ).all()
    left = team_totals(entries, src.id, period).remaining
    if left < amount:
        return _failed(f"{src.name} only has ${money(max(left, Decimal(0)))} left this period, "
                       f"so it cannot give ${amount}. Propose a smaller amount or none.")

    payload = {"from_team_id": str(src.id), "to_team_id": str(dst.id), "amount_usd": str(amount)}
    return _create(session, "propose_budget_change", f"{src.id}->{dst.id}", reason, src, payload)