"""A6 + A7 endpoints (Person 3): escalations, notifications and reports.

Thin layer over the tested services: EscalationService (A6) and ReportService (A7) do the
work and the role rules; this file turns the web request into an Actor, calls them and
shapes the answer like docs/contract/openapi.yaml (UUID ids, money as 4-decimal strings).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlmodel import select

from app.core.money import money_str
from app.core.scoping import Scope, ScopeDep
from app.insight import models as m
from app.insight import reports as rep
from app.insight.escalations import InvalidState, PermissionDenied
from app.insight.wiring import escalation_service, report_service
from app.models import Notification as NotificationRow

router = APIRouter(tags=["insight"])


# ====================================================================== who is asking
def _actor(me) -> m.Actor:
    role = m.ROLE_DEPARTMENT_ADMIN if me.is_admin else m.ROLE_PROFESSOR if me.professor_id else m.ROLE_USER
    dept = sorted(map(str, me.admin_department_ids))
    return m.Actor(
        user_id=str(me.id), role=role,
        professor_id=str(me.professor_id) if me.professor_id else None,
        department_id=dept[0] if dept else None,
    )


def _can_act(me):
    """The recipient decides. A person can be a professor and a department admin at once
    (D21), so we check the professor id AND every department he administers."""
    admin_depts = {str(d) for d in me.admin_department_ids}
    prof = str(me.professor_id) if me.professor_id else None

    def can_act(actor: m.Actor, e: m.Escalation) -> bool:
        if e.recipient_type == m.PROFESSOR:
            return prof is not None and prof == str(e.recipient_id)
        return str(e.recipient_id) in admin_depts

    return can_act


def _service(scope: Scope):
    # Real approval actions (stop pod, disable key, change budget) plug in as `executors`.
    from app.insight.executors import build_executors

    return escalation_service(scope.session, build_executors(scope.session, scope.me), can_act=_can_act(scope.me))


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ====================================================================== escalations
class EscalationOut(BaseModel):
    id: uuid.UUID
    kind: str
    target: str
    reason: str
    recipient_type: str
    group_id: Optional[uuid.UUID] = None
    status: str
    created_at: datetime
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    result: Optional[dict[str, Any]] = None


class RejectIn(BaseModel):
    note: str = ""


def _escalation_out(e: m.Escalation) -> EscalationOut:
    return EscalationOut(
        id=e.id, kind=e.kind, target=e.target, reason=e.reason, recipient_type=e.recipient_type,
        group_id=e.team_id, status=e.status, created_at=_utc(e.created_at),
        decided_at=_utc(e.decided_at), note=e.note or None, result=e.result,
    )


@router.get("/escalations", response_model=list[EscalationOut])
def list_escalations(scope: ScopeDep, status_filter: Optional[str] = Query(None, alias="status")) -> list[EscalationOut]:
    if status_filter is not None and status_filter not in (m.PENDING, m.APPROVED, m.REJECTED, m.FAILED):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "status must be pending, approved, rejected or failed")
    svc = _service(scope)
    return [_escalation_out(e) for e in svc.list_for(_actor(scope.me), status_filter)]


def _decide(scope: Scope, escalation_id: uuid.UUID, action: str, note: str = "") -> EscalationOut:
    svc = _service(scope)
    if svc.store.get_escalation(str(escalation_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")
    actor = _actor(scope.me)
    try:
        e = svc.approve(str(escalation_id), actor) if action == "approve" else svc.reject(str(escalation_id), actor, note)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except InvalidState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    scope.session.commit()  # the decision was saved by the store; this saves its audit entry
    return _escalation_out(e)


@router.post("/escalations/{escalation_id}/approve", response_model=EscalationOut)
def approve_escalation(escalation_id: uuid.UUID, scope: ScopeDep) -> EscalationOut:
    return _decide(scope, escalation_id, "approve")


@router.post("/escalations/{escalation_id}/reject", response_model=EscalationOut)
def reject_escalation(escalation_id: uuid.UUID, scope: ScopeDep, body: Optional[RejectIn] = None) -> EscalationOut:
    return _decide(scope, escalation_id, "reject", (body.note if body else ""))


# ====================================================================== notifications
class NotificationOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    message: str
    level: str
    read: bool
    created_at: datetime


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(scope: ScopeDep, unread_only: bool = False) -> list[NotificationOut]:
    q = select(NotificationRow).where(NotificationRow.group_id.in_(scope.group_ids()))  # type: ignore[attr-defined]
    if unread_only:
        q = q.where(NotificationRow.read == False)  # noqa: E712
    q = q.order_by(NotificationRow.created_at.desc())  # type: ignore[attr-defined]
    return [NotificationOut.model_validate(r, from_attributes=True) for r in scope.session.exec(q).all()]


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(notification_id: uuid.UUID, scope: ScopeDep) -> NotificationOut:
    row = scope.session.exec(
        select(NotificationRow).where(
            NotificationRow.id == notification_id,
            NotificationRow.group_id.in_(scope.group_ids()),  # type: ignore[attr-defined]
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    row.read = True
    scope.session.add(row)
    scope.session.commit()
    scope.session.refresh(row)
    return NotificationOut.model_validate(row, from_attributes=True)


# ====================================================================== reports
def _m(value: Any) -> str:
    return money_str(Decimal(str(value)))


def _forbidden(exc: PermissionDenied) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))


def _billing_row(r: dict) -> dict:
    out = {"professor_id": str(r["professor_id"]), "professor_name": r.get("professor_name", "")}
    for k in ("billable", "observed", "unassigned", "total", "openrouter", "runpod"):
        out[k] = _m(r[k])
    return out


@router.get("/reports/weekly")
def weekly(scope: ScopeDep, week_start: Optional[date] = None) -> dict:
    if week_start is None:  # the Monday of the last full week
        today = datetime.now(timezone.utc).date()
        week_start = today - timedelta(days=today.weekday() + 7)
    actor = _actor(scope.me)
    try:
        mine = _service(scope).list_for(actor)
        r = report_service(scope.session).weekly_report(actor, week_start, escalations=mine)
    except PermissionDenied as exc:
        raise _forbidden(exc) from None
    rows = [
        {
            "professor_id": str(p["professor_id"]), "professor_name": p["professor_name"],
            "this_week_usd": _m(p["this_week_usd"]), "last_week_usd": _m(p["last_week_usd"]),
            "change_pct": p["change_pct"],
            "billable_usd": _m(p["billable_usd"]), "observed_usd": _m(p["observed_usd"]),
            "groups": [
                {"group_id": g["team_id"], "this_week_usd": _m(g["this_week_usd"]),
                 "last_week_usd": _m(g["last_week_usd"]), "change_pct": g["change_pct"]}
                for g in p["teams"]
            ],
        }
        for p in r["rows"]
    ]
    t = r["totals"]
    return {
        "week_start": r["week_start"], "rows": rows,
        "totals": {"this_week_usd": _m(t["this_week_usd"]), "last_week_usd": _m(t["last_week_usd"]),
                   "change_pct": t["change_pct"]},
        "escalations": r["escalations"],
    }


@router.get("/reports/billing")
def billing(scope: ScopeDep, period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
            format: str = Query("json", pattern="^(json|csv)$")):
    try:
        r = report_service(scope.session).billing_report(_actor(scope.me), period)
    except PermissionDenied as exc:
        raise _forbidden(exc) from None
    if format == "csv":
        return Response(
            rep.billing_report_csv(r), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="billing-{period}.csv"'},
        )
    return {"period": r["period"], "rows": [_billing_row(x) for x in r["rows"]], "totals": _billing_row(r["totals"])}


@router.get("/reports/trend")
def trend(scope: ScopeDep) -> list[dict]:
    try:
        rows = report_service(scope.session).trend(_actor(scope.me))
    except PermissionDenied as exc:
        raise _forbidden(exc) from None
    return [{"period": x["period"], **{k: _m(x[k]) for k in ("billable", "observed", "unassigned", "total")}} for x in rows]
