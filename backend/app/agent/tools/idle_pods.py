"""list_idle_pods (Person 1, Week 3): running pods of one team whose GPU has sat unused.

RunPod only tells us the GPU utilisation at the moment we ask (runtime.gpus[0].util). "Idle for
40 hours" therefore needs a history, which we keep on the pod's own row:

    details["gpu_idle_since"]     ISO time of the first reading at or below IDLE_UTIL_PCT,
                                  cleared as soon as a reading shows the GPU working again
    details["gpu_util_checked_at"] ISO time of the last reading
    details["gpu_util_percent"]   the last reading

track_idle_pods() takes one reading for every running pod. It is meant to run on every sync
(every 5 minutes), which gives the history a 5-minute resolution. The agent tool also takes a
fresh reading for its own team before answering, so it works even before the sync calls it;
then the idle time counts from the first reading.

The shape matches evaluation/world/sim_server.py: {"status", "team_id", "idle_pods": [...]}.
Ids are issued_resources ids, the ones propose_pod_stop and the stop executor take.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from sqlmodel import Session, select

from app.models import Group, IssuedResource, Provider, ResourceStatus, User

IDLE_UTIL_PCT = 5          # at or below this the GPU counts as unused
IDLE_HOURS_THRESHOLD = 24  # same threshold as the evaluation world


# ------------------------------------------------------------------ the idle history (pure)
def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def record_gpu_reading(res: IssuedResource, util_percent: int | None, now: datetime) -> bool:
    """Fold one GPU reading into the pod's idle history. Returns True if details changed.

    None means RunPod gave no reading (pod still starting, no runtime): we keep what we had.
    """
    if util_percent is None:
        return False
    d = dict(res.details or {})
    d["gpu_util_percent"] = int(util_percent)
    d["gpu_util_checked_at"] = now.isoformat()
    if util_percent <= IDLE_UTIL_PCT:
        d.setdefault("gpu_idle_since", now.isoformat())
    else:
        d.pop("gpu_idle_since", None)
    res.details = d  # JSONB: assign a new dict so SQLAlchemy sees the change
    return True


def idle_hours(res: IssuedResource, now: datetime) -> float:
    """Hours the GPU of a running pod has been unused, from its recorded history. 0 if unknown.

    The pod's own (re)start time bounds it: a pod restarted an hour ago cannot have been
    idle for longer, even if an old gpu_idle_since was left behind.
    """
    if res.status != ResourceStatus.running:
        return 0.0
    d = res.details or {}
    since = _parse(d.get("gpu_idle_since"))
    if since is None:
        return 0.0
    started = _parse(d.get("started_at"))
    if started and started > since:
        since = started
    return round(max((now - since).total_seconds(), 0) / 3600, 1)


# ------------------------------------------------------------------ taking readings
def _default_adapter_for(session: Session) -> Callable[[uuid.UUID], Any]:
    from app.jobs.watchdog import Lookup  # one adapter per credential, cached

    lookup = Lookup(session)
    return lookup.adapter


def track_idle_pods(
    session: Session,
    now: datetime | None = None,
    *,
    pods: Iterable[IssuedResource] | None = None,
    adapter_for: Callable[[uuid.UUID], Any] | None = None,
) -> dict[str, int]:
    """Read the GPU utilisation of every running pod (or of `pods`) and update its history.

    For the sync job: one call per cycle, after usage. It never raises for a single pod; a
    pod RunPod cannot answer for keeps its history and is counted in "failed". Does not
    commit: the caller does.
    """
    now = now or datetime.now(timezone.utc)
    adapter_for = adapter_for or _default_adapter_for(session)
    if pods is None:
        pods = session.exec(
            select(IssuedResource).where(
                IssuedResource.provider == Provider.runpod,
                IssuedResource.status == ResourceStatus.running,
            )
        ).all()
    out = {"read": 0, "failed": 0}
    for res in pods:
        if res.provider != Provider.runpod or res.status != ResourceStatus.running:
            continue
        try:
            adapter = adapter_for(res.credential_id)
            status = adapter.pod_status(res.external_ref) if adapter is not None else None
        except Exception:  # provider down, pod gone: the sync's own logic deals with that
            status = None
        if status is None:
            out["failed"] += 1
            continue
        if record_gpu_reading(res, getattr(status, "gpu_util_percent", None), now):
            session.add(res)
            out["read"] += 1
    return out


# ------------------------------------------------------------------ the agent tool
def tool_list_idle_pods(
    session: Session,
    team_id: str,
    now: datetime | None = None,
    *,
    adapter_for: Callable[[uuid.UUID], Any] | None = None,
    min_idle_hours: float = IDLE_HOURS_THRESHOLD,
) -> dict:
    """Agent tool: running pods of one team whose GPU has been unused for min_idle_hours."""
    try:
        gid = uuid.UUID(str(team_id))
    except ValueError:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    if session.get(Group, gid) is None:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    now = now or datetime.now(timezone.utc)

    running = session.exec(
        select(IssuedResource).where(
            IssuedResource.group_id == gid,
            IssuedResource.provider == Provider.runpod,
            IssuedResource.status == ResourceStatus.running,
        )
    ).all()
    if running:
        # A fresh reading, so the answer is current even between syncs. The history is only
        # observation, never an action, so it is safe to save here.
        if track_idle_pods(session, now, pods=running, adapter_for=adapter_for)["read"]:
            session.commit()

    names = {u.id: u.name for u in session.exec(
        select(User).where(User.id.in_({r.user_id for r in running}))  # type: ignore[attr-defined]
    ).all()} if running else {}

    idle = []
    for r in running:
        hours = idle_hours(r, now)
        if hours < min_idle_hours:
            continue
        d = r.details or {}
        hourly = float(d["cost_per_hr"]) if d.get("cost_per_hr") is not None else None
        idle.append({
            "id": str(r.id),
            "user": names.get(r.user_id),
            "gpu_type": d.get("gpu_type"),
            "hourly_usd": hourly,
            "idle_hours": hours,
            # What the idle time has cost so far; arithmetic stays in the tool, not the model.
            "idle_cost_usd": round(hours * hourly, 2) if hourly is not None else None,
            "last_gpu_util_percent": d.get("gpu_util_percent"),
        })
    idle.sort(key=lambda p: p["idle_cost_usd"] or 0, reverse=True)
    return {"status": "ok", "team_id": str(gid), "idle_threshold_hours": min_idle_hours,
            "idle_pods": idle}
