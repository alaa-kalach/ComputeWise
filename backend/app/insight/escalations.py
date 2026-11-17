"""Escalations (A6): what the agent proposes, and what a human decides.

The agent never changes anything. It creates an escalation; a professor or the
department admin approves or rejects it, and only then is the action carried out.

Rules enforced here:
  * Routing: pod, key and reallocation proposals go to the team's professor;
    escalate_to_department goes to the department.
  * Cooldown: the same issue (same kind and target) is not raised again while an
    earlier one is pending or was created within `cooldown_hours`.
  * Permissions are re-checked when the decision is made, not only when the
    proposal was created (a professor who lost the team in the meantime is refused).
  * Every decision, and every failure, is written to the audit log.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Optional

from app.insight import models as m

DEFAULT_COOLDOWN_HOURS = 24


class PermissionDenied(Exception):
    pass


class InvalidState(Exception):
    pass


def _noop_audit(actor_id, action, target_type, target_id, details=None):
    """Default when no audit is wired in (unit tests). In the app, use
    app.insight.wiring.escalation_service(session): it writes to the real audit_log table.
    Action names follow the audit convention "<thing>.<verb>": escalation.create,
    escalation.approve, escalation.reject, escalation.fail."""


def _acknowledge(escalation):
    return {"acknowledged": True}


def default_can_act(actor: m.Actor, escalation: m.Escalation) -> bool:
    """The recipient may decide. Decided by id, not by role label, because one person
    can be both a department admin and a professor (a sub-admin): an admin who wants
    to open keys is also given the professor role. Replace with a live ownership
    check in production."""
    if escalation.recipient_type == m.PROFESSOR:
        return actor.professor_id is not None and actor.professor_id == escalation.recipient_id
    return actor.department_id is not None and actor.department_id == escalation.recipient_id


class EscalationService:
    def __init__(self, store, executors: Optional[dict] = None,
                 write_audit: Callable = _noop_audit,
                 can_act: Callable = default_can_act,
                 now: Callable[[], datetime] = datetime.utcnow,
                 cooldown_hours: int = DEFAULT_COOLDOWN_HOURS):
        self.store = store
        # kind -> function(escalation) -> dict. Person 1 and 2's adapters plug in here.
        self.executors = {"escalate_to_department": _acknowledge, **(executors or {})}
        self.write_audit = write_audit
        self.can_act = can_act
        self.now = now
        self.cooldown = timedelta(hours=cooldown_hours)

    # ------------------------------------------------------------ create

    def create(self, kind, target, reason, professor_id, department_id=None,
               team_id=None, payload=None, actor_id="agent"):
        """Returns (escalation, created). created is False when the cooldown suppressed it."""
        if kind not in m.KINDS:
            raise ValueError(f"unknown escalation kind: {kind}")
        if kind == "escalate_to_department":
            if not department_id:
                raise ValueError("escalate_to_department needs a department_id")
            recipient_type, recipient_id = m.DEPARTMENT, department_id
        else:
            recipient_type, recipient_id = m.PROFESSOR, professor_id

        existing = self._recent_same_issue(kind, target, recipient_id)
        if existing:
            return existing, False

        e = self.store.add_escalation(
            kind=kind, target=target, reason=reason, recipient_type=recipient_type,
            recipient_id=recipient_id, created_at=self.now(), team_id=team_id,
            payload=payload or {})
        self.write_audit(actor_id, "escalation.create", "escalation", e.id,
                         {"kind": kind, "target": target, "recipient": recipient_id})
        return e, True

    def _recent_same_issue(self, kind, target, recipient_id):
        cutoff = self.now() - self.cooldown
        for e in self.store.escalations():
            if (e.kind, e.target, e.recipient_id) != (kind, target, recipient_id):
                continue
            if e.status == m.PENDING or e.created_at >= cutoff:
                return e
        return None

    # ------------------------------------------------------------ decide

    def approve(self, escalation_id, actor: m.Actor):
        e = self._pending_for(escalation_id, actor)
        e.status, e.decided_by, e.decided_at = m.APPROVED, actor.user_id, self.now()
        executor = self.executors.get(e.kind)
        try:
            if executor is None:
                raise RuntimeError(f"no executor for {e.kind}")
            e.result = executor(e)
        except Exception as exc:                     # noqa: BLE001  (record, do not crash)
            e.status, e.result = m.FAILED, {"error": str(exc)}
            self.store.save_escalation(e)
            self.write_audit(actor.user_id, "escalation.fail", "escalation", e.id,
                             {"error": str(exc)})
            return e
        self.store.save_escalation(e)
        self.write_audit(actor.user_id, "escalation.approve", "escalation", e.id,
                         {"kind": e.kind, "target": e.target, "result": e.result})
        return e

    def reject(self, escalation_id, actor: m.Actor, note=""):
        e = self._pending_for(escalation_id, actor)
        e.status, e.decided_by, e.decided_at, e.note = (
            m.REJECTED, actor.user_id, self.now(), note)
        self.store.save_escalation(e)
        self.write_audit(actor.user_id, "escalation.reject", "escalation", e.id,
                         {"kind": e.kind, "target": e.target, "note": note})
        return e

    def _pending_for(self, escalation_id, actor):
        e = self.store.get_escalation(escalation_id)
        if e is None:
            raise InvalidState(f"escalation {escalation_id} does not exist")
        if not self.can_act(actor, e):               # re-checked now, not at creation
            raise PermissionDenied("you cannot decide this escalation")
        if e.status != m.PENDING:
            raise InvalidState(f"escalation {escalation_id} is already {e.status}")
        return e

    # ------------------------------------------------------------ read

    def list_for(self, actor: m.Actor, status: Optional[str] = None):
        """Escalations this actor may decide, newest first."""
        found = [e for e in self.store.escalations()
                 if self.can_act(actor, e) and (status is None or e.status == status)]
        return sorted(found, key=lambda e: e.created_at, reverse=True)
