"""The real actions behind an approved escalation (A6 follow-up).

    propose_pod_stop      target = the pod's id          -> stop the pod on RunPod
    propose_key_disable   target = the key's id          -> disable the key on OpenRouter
    propose_budget_change payload = from_team_id, to_team_id, amount_usd
                                                         -> move budget between two teams
    escalate_to_department  has no action (approving it only acknowledges it)

They reuse the adapters and the ledger of Persons 1 and 2, and write the same audit entries
as the matching manual actions. The permission is checked AGAIN here: the approving
professor must own the group the action touches. Any error is raised, so the escalation
becomes `failed` with the reason in `result` (nothing is half done: a failure rolls back).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Callable

from sqlmodel import Session, select

from app.access.credentials import adapter_for_credential
from app.adapters import AdapterError
from app.budget.ledger import money, period_of, team_totals
from app.budget.topups import correction_entry
from app.core.audit import write_audit
from app.models import Group, IssuedResource, LedgerEntry, Provider, ResourceStatus, ScopeType
from app.models.base import utcnow


def _own_group(session: Session, me, group_id) -> Group:
    group = session.get(Group, group_id)
    if group is None:
        raise RuntimeError("that team no longer exists")
    if me.professor_id is None or group.professor_id != me.professor_id:
        raise PermissionError("you do not manage that team")
    return group


def _resource(session: Session, target: str, provider: Provider) -> IssuedResource:
    res = None
    try:
        res = session.get(IssuedResource, uuid.UUID(str(target)))
    except ValueError:
        pass
    if res is None:
        res = session.exec(
            select(IssuedResource).where(IssuedResource.provider == provider, IssuedResource.external_ref == str(target))
        ).first()
    if res is None or res.provider != provider:
        raise RuntimeError(f"no {provider.value} resource with id {target}")
    return res


def build_executors(session: Session, me) -> dict[str, Callable]:
    def safely(fn: Callable) -> Callable:
        def run(e):
            try:
                return fn(e)
            except Exception:
                session.rollback()
                raise
        return run

    def stop_pod(e) -> dict:
        res = _resource(session, e.target, Provider.runpod)
        _own_group(session, me, res.group_id)
        if res.status != ResourceStatus.running:
            raise RuntimeError(f"the pod is already {res.status.value}")
        adapter = adapter_for_credential(session, res.credential_id)
        if adapter is None:
            raise RuntimeError("the credential this pod was launched with was revoked")
        try:
            adapter.stop(res.external_ref)
        except AdapterError as exc:
            raise RuntimeError(f"RunPod refused: {exc}") from None
        res.status = ResourceStatus.stopped
        res.details = {**res.details, "ready": None, "stopped_by": "escalation"}
        session.add(res)
        write_audit(session, me, "pod.stop", "issued_resource", res.id,
                    {"group_id": res.group_id, "user_id": res.user_id, "by": "escalation", "escalation_id": e.id})
        return {"stopped_pod": str(res.id)}

    def disable_key(e) -> dict:
        res = _resource(session, e.target, Provider.openrouter)
        _own_group(session, me, res.group_id)
        if res.status == ResourceStatus.disabled:
            raise RuntimeError("the key is already disabled")
        adapter = adapter_for_credential(session, res.credential_id)
        if adapter is None:
            raise RuntimeError("the credential this key was issued under was revoked")
        try:
            adapter.stop(res.external_ref)  # OpenRouter: disable (keeps usage and limit)
        except AdapterError as exc:
            raise RuntimeError(f"OpenRouter refused: {exc}") from None
        res.status = ResourceStatus.disabled
        session.add(res)
        write_audit(session, me, "key.disable", "issued_resource", res.id,
                    {"group_id": res.group_id, "user_id": res.user_id, "by": "escalation", "escalation_id": e.id})
        return {"disabled_key": str(res.id)}

    def move_budget(e) -> dict:
        p = e.payload or {}
        try:
            src_id, dst_id = uuid.UUID(str(p["from_team_id"])), uuid.UUID(str(p["to_team_id"]))
            amount = money(Decimal(str(p["amount_usd"])))
        except (KeyError, ValueError, ArithmeticError):
            raise RuntimeError("the proposal has no valid from_team_id, to_team_id and amount_usd") from None
        if amount <= 0 or src_id == dst_id:
            raise RuntimeError("the amount must be positive and the two teams different")
        src, dst = _own_group(session, me, src_id), _own_group(session, me, dst_id)
        period = period_of(utcnow())
        entries = list(session.exec(
            select(LedgerEntry).where(LedgerEntry.group_id == src.id, LedgerEntry.period == period)).all())
        left = team_totals(entries, src.id, period).remaining
        if left < amount:
            raise RuntimeError(f"that team only has ${money(left)} left, not ${amount}")
        note = f"moved ${amount} from {src.name} to {dst.name} (escalation {e.id})"
        out = correction_entry(ScopeType.group, src.id, period, -amount, group_id=src.id, created_by=me.id, note=note)
        into = correction_entry(ScopeType.group, dst.id, period, amount, group_id=dst.id, created_by=me.id, note=note)
        session.add(out)
        session.add(into)
        session.flush()
        for g, entry, kind in ((src, out, "move_out"), (dst, into, "move_in")):
            write_audit(session, me, "ledger.adjustment", "group", g.id,
                        {"kind": kind, "amount": entry.amount, "note": entry.note, "entry_id": entry.id})
        return {"moved_usd": str(amount), "from_team": str(src.id), "to_team": str(dst.id)}

    return {
        "propose_pod_stop": safely(stop_pod),
        "propose_key_disable": safely(disable_key),
        "propose_budget_change": safely(move_budget),
    }
