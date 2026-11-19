"""get_usage (Person 1, Week 3): spend of one team per key and per pod, with the top drivers.

The agent uses this tool to explain WHY a team spends fast ("most of this week's spend is one
A100 pod"). It only reads. The shape matches evaluation/world/sim_server.py, so the agent sees
the same thing in the evaluation and on the real database:

    {"status": "ok", "team_id": ..., "resources": [row, ...], "top_drivers": [...], ...}

Every id is the issued_resources id, the same id the ledger's resource_id holds. So
detect_spend_anomalies, list_idle_pods, the proposal tools and the escalation executors all
talk about the same resource, and the evidence gates can match them.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session, select

from app.budget.ledger import period_of
from app.models import Group, IssuedResource, LedgerEntry, LedgerType, Provider, ResourceStatus, User

from .idle_pods import idle_hours

LAST_DAYS = 7
TOP_N = 3


def _team_id(team_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(team_id))
    except ValueError:
        return None


def _usd(x: Decimal | float) -> float:
    return round(float(x), 2)


def _label(res: IssuedResource, user_name: str | None) -> str:
    """A short human name for warnings: "Omar's RTX A4000 pod", "Sara's key sara-fyp1"."""
    who = f"{user_name}'s" if user_name else "a"
    d = res.details or {}
    if res.provider == Provider.runpod:
        gpu = d.get("gpu_type")
        return f"{who} {gpu} pod" if gpu else f"{who} pod"
    name = d.get("name")
    return f"{who} key {name}" if name else f"{who} OpenRouter key"


def tool_get_usage(session: Session, team_id: str, now: datetime | None = None) -> dict:
    """Agent tool: one team's spend this period per key and pod, and the top spend drivers."""
    gid = _team_id(team_id)
    if gid is None or session.get(Group, gid) is None:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    now = now or datetime.now(timezone.utc)
    period = period_of(now)
    week_start = now - timedelta(days=LAST_DAYS)

    # Spend entries of this team: this period, plus the last 7 days (which may reach back
    # into last month early in a month). Spend is stored negative; flip the sign.
    spend = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.group_id == gid,
            LedgerEntry.type == LedgerType.spend,
            (LedgerEntry.period == period) | (LedgerEntry.created_at >= week_start),
        )
    ).all()
    period_by_res: dict[uuid.UUID | None, Decimal] = defaultdict(Decimal)
    week_by_res: dict[uuid.UUID | None, Decimal] = defaultdict(Decimal)
    for e in spend:
        if e.period == period:
            period_by_res[e.resource_id] += -e.amount
        if e.created_at >= week_start and e.created_at <= now:
            week_by_res[e.resource_id] += -e.amount

    # Every key and pod of the team that is live or spent something in the window.
    spent_ids = {r for r in set(period_by_res) | set(week_by_res) if r is not None}
    resources = session.exec(select(IssuedResource).where(IssuedResource.group_id == gid)).all()
    gone = (ResourceStatus.terminated, ResourceStatus.disabled)
    resources = [r for r in resources if r.id in spent_ids or r.status not in gone]
    names = {u.id: u.name for u in session.exec(
        select(User).where(User.id.in_({r.user_id for r in resources}))  # type: ignore[attr-defined]
    ).all()} if resources else {}

    rows = []
    for r in resources:
        d = r.details or {}
        user = names.get(r.user_id)
        row = {
            "id": str(r.id),
            "type": "pod" if r.provider == Provider.runpod else "key",
            "label": _label(r, user),
            "user": user,
            "status": r.status.value,
            "spent_usd": _usd(period_by_res.get(r.id, Decimal(0))),
            "last_7_days_usd": _usd(week_by_res.get(r.id, Decimal(0))),
        }
        if r.provider == Provider.runpod:
            row.update(
                gpu_type=d.get("gpu_type"),
                hourly_usd=float(d["cost_per_hr"]) if d.get("cost_per_hr") is not None else None,
                idle_hours=idle_hours(r, now) if r.status == ResourceStatus.running else 0.0,
            )
        else:
            row.update(limit_usd=_usd(r.current_limit) if r.current_limit is not None else None)
        rows.append(row)
    rows.sort(key=lambda x: (x["last_7_days_usd"], x["spent_usd"]), reverse=True)

    total7 = sum(week_by_res.values(), Decimal(0))
    drivers = [x for x in rows if x["last_7_days_usd"] > 0][:TOP_N]
    drivers_basis = "last_7_days"
    if not drivers:
        # Nothing spent in the last 7 days, but maybe earlier this month: rank by the month,
        # so a warning can still name what drove the spend (the evidence gate needs names).
        drivers = sorted((x for x in rows if x["spent_usd"] > 0),
                         key=lambda x: x["spent_usd"], reverse=True)[:TOP_N]
        drivers_basis = "this_period"
    total_period = float(sum(period_by_res.values(), Decimal(0)))

    def driver(x: dict) -> dict:
        d = {"id": x["id"], "label": x["label"], "type": x["type"],
             "last_7_days_usd": x["last_7_days_usd"], "spent_usd": x["spent_usd"],
             "share_of_last_7_days": round(x["last_7_days_usd"] / float(total7), 2) if total7 else 0.0}
        if drivers_basis == "this_period":
            d["share_of_period"] = round(x["spent_usd"] / total_period, 2) if total_period else 0.0
        return d

    return {
        "status": "ok",
        "team_id": str(gid),
        "period": period,
        "spent_usd": _usd(sum(period_by_res.values(), Decimal(0))),
        "last_7_days_usd": _usd(total7),
        # Spend the ledger could not tie to a key or pod (rare; e.g. a manual correction).
        "unattributed_usd": _usd(period_by_res.get(None, Decimal(0))),
        "resources": rows,
        # "last_7_days" normally; "this_period" when nothing was spent in the last 7 days.
        "top_drivers_basis": drivers_basis,
        "top_drivers": [driver(x) for x in drivers],
    }
