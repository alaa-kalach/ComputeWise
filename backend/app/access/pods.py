"""RunPod pods through ComputeWise (B4).

    GET  /runpod/gpus?group_id=   launch form: GPUs with live price, availability, and how many
                                  hours the caller's budget covers
    GET  /pods                    pods in scope (student: his own; professor: his groups';
                                  department: all, read only)
    POST /pods                    a student with approved RunPod access launches his own pod
    GET  /pods/{id}               one pod, with SSH/Jupyter details once it is ready
    POST /pods/{id}/stop          the owner or the group's professor; volume kept (D3)
    POST /pods/{id}/start         the owner or the group's professor; same pre-launch check
    DELETE /pods/{id}             terminate (owner or professor); deletes the volume

Who may do what (D21): the student launches with his OWN approved access
(require_approved_access, not the professor-only check). Stop/start/terminate of someone
else's pod needs scope.require_manage_group: only the group's professor; the department
observes only.

Pre-launch check (D16): refused unless the caller's available budget in that group (team
remaining, limited by his own user budget if he has one) covers at least 1 hour of GPU +
storage, and the GPU's availability is not "none". RunPod itself needs 1 hour of credit.

State: issued_resources.status is running/stopped/terminated. "starting" vs "running" is
details["ready"], set the first time GET /pods/{id} sees the pod's runtime. The sync job
(Person 2) is what moves pods to stopped/terminated when RunPod changes them on its own;
GET /pods/{id} shows the live state but only records readiness.

Every launch, stop, start and terminate writes an audit entry (D12).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.access.credentials import adapter_for_credential, resolve_credential
from app.access.requests import require_approved_access
from app.adapters import AdapterError, IssueRequest, ProviderUnavailable, ResourceNotFound, get_adapter
from app.adapters.runpod import GpuOffer, InsufficientBalance, NoCapacity, PodStatus, hourly_rates
from app.budget.ledger import available_to_user, period_of, team_totals, user_totals
from app.core.audit import write_audit
from app.core.money import Money, money_str, quantize
from app.core.scoping import Scope, ScopeDep
from app.models import (
    BudgetPolicy,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    Provider,
    ResourceStatus,
    ScopeType,
    User,
)
from app.models.base import utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["pods"])

MIN_HOURS = Decimal("1")  # D16: RunPod's own minimum credit to start a pod
GPU_CACHE_SECONDS = 300
Cloud = Literal["secure", "community"]
PodState = Literal["starting", "running", "stopped", "terminated"]


# ====================================================================== API models
class GpuOptionOut(BaseModel):
    gpu_type_id: str
    name: str
    vram_gb: int
    cloud: Cloud
    price_per_hour: Money
    availability: Literal["none", "low", "medium", "high"]
    affordable_hours: str | None  # e.g. "38.5"; null if the price is 0


class PodLaunchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: uuid.UUID
    gpu_type_id: str = Field(min_length=1, max_length=100)
    cloud: Cloud
    gpu_count: int = Field(default=1, ge=1, le=1)
    container_disk_gb: int = Field(default=10, ge=10, le=100)
    volume_gb: int = Field(default=10, ge=10, le=200)
    jupyter: bool = True


class PodOut(BaseModel):
    id: uuid.UUID
    external_ref: str
    user_id: uuid.UUID
    user_name: str
    group_id: uuid.UUID
    gpu_name: str
    cloud: Cloud
    price_per_hour: Money
    storage_per_hour: Money  # while stopped (the volume)
    container_disk_gb: int
    volume_gb: int
    state: PodState
    started_at: datetime | None
    stopped_by: Literal["owner", "professor", "watchdog"] | None
    spent_this_month_usd: Money
    created_at: datetime


class ConnectionOut(BaseModel):
    ssh_proxy: str | None
    ssh_direct: str | None
    jupyter_url: str | None
    jupyter_password: str | None  # the pod's owner only


class PodDetailOut(PodOut):
    connection: ConnectionOut | None  # only while running
    gpu_util_percent: int | None


# ====================================================================== refusals (409 LaunchRefused)
class LaunchRefused(Exception):
    def __init__(self, reason: str, detail: str, needed: Decimal | None = None, remaining: Decimal | None = None):
        super().__init__(detail)
        self.reason, self.detail, self.needed, self.remaining = reason, detail, needed, remaining

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": self.detail,
                "reason": self.reason,
                "needed_usd": money_str(self.needed) if self.needed is not None else None,
                "remaining_usd": money_str(self.remaining) if self.remaining is not None else None,
            },
        )


def _provider_error(exc: AdapterError) -> HTTPException:
    if isinstance(exc, ProviderUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "RunPod can't be reached right now. Try again shortly.")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"RunPod refused the request: {exc}")


# ====================================================================== budget (for the pre-launch check)
def available_budget(session: Session, group_id: uuid.UUID, user_id: uuid.UUID | None) -> Decimal:
    """What can still be spent this month in a group: the team's remaining, limited by the
    user's own budget if he has a user policy there (D9). user_id None = team only."""
    period = period_of(utcnow())
    entries = list(
        session.exec(select(LedgerEntry).where(LedgerEntry.group_id == group_id, LedgerEntry.period == period)).all()
    )
    team = team_totals(entries, group_id, period)
    user = None
    if user_id is not None:
        has_policy = session.exec(
            select(BudgetPolicy.id).where(
                BudgetPolicy.scope_type == ScopeType.user,
                BudgetPolicy.scope_id == user_id,
                BudgetPolicy.group_id == group_id,
            )
        ).first()
        if has_policy is not None:
            user = user_totals(entries, user_id, group_id, period)
    return available_to_user(team, user)


def prelaunch_check(session: Session, group_id: uuid.UUID, user_id: uuid.UUID, hourly: Decimal) -> None:
    """LaunchRefused("budget") unless the budget covers MIN_HOURS of this pod."""
    needed = hourly * MIN_HOURS
    remaining = available_budget(session, group_id, user_id)
    if remaining < needed:
        raise LaunchRefused(
            "budget",
            f"Not enough budget: this pod needs ${money_str(needed)} for 1 hour; ${money_str(remaining)} is left.",
            needed,
            remaining,
        )


# ====================================================================== GPU catalog (cached)
_gpu_cache: dict[tuple[uuid.UUID, str], tuple[float, list[GpuOffer]]] = {}


def _gpus(adapter: Any, credential_id: uuid.UUID, cloud: str) -> list[GpuOffer]:
    key = (credential_id, cloud)
    hit = _gpu_cache.get(key)
    if hit and time.monotonic() - hit[0] < GPU_CACHE_SECONDS:
        return hit[1]
    offers = adapter.list_gpus(cloud)
    _gpu_cache[key] = (time.monotonic(), offers)
    return offers


def _hours(remaining: Decimal, hourly: Decimal) -> str | None:
    if hourly <= 0:
        return None
    return str((max(remaining, Decimal(0)) / hourly).quantize(Decimal("0.1"), rounding=ROUND_DOWN))


@router.get("/runpod/gpus", response_model=list[GpuOptionOut])
def list_gpus(group_id: uuid.UUID, scope: ScopeDep) -> list[GpuOptionOut]:
    """Both clouds, cheapest first. affordable_hours uses the default 10 GB disk + 10 GB volume."""
    group = scope.require_group(group_id)
    cred = resolve_credential(scope.session, group.professor_id, Provider.runpod)  # 409 if none
    adapter = get_adapter(Provider.runpod, cred.secret)
    is_member = scope.session.exec(
        select(GroupMember.user_id).where(GroupMember.group_id == group.id, GroupMember.user_id == scope.me.id)
    ).first() is not None
    remaining = available_budget(scope.session, group.id, scope.me.id if is_member else None)
    out: list[GpuOptionOut] = []
    try:
        for cloud in ("community", "secure"):
            for o in _gpus(adapter, cred.credential_id, cloud):
                rates = hourly_rates(o.price_per_hour, 1, 10, 10)
                out.append(
                    GpuOptionOut(
                        gpu_type_id=o.gpu_type_id, name=o.name, vram_gb=o.vram_gb, cloud=o.cloud,  # type: ignore[arg-type]
                        price_per_hour=quantize(o.price_per_hour), availability=o.availability,  # type: ignore[arg-type]
                        affordable_hours=_hours(remaining, rates.running),
                    )
                )
    except AdapterError as exc:
        raise _provider_error(exc) from None
    return sorted(out, key=lambda g: (g.price_per_hour, g.name, g.cloud))


# ====================================================================== pod helpers
@dataclass
class _Ctx:
    res: IssuedResource
    as_owner: bool  # False = acting as the group's professor


def _require_pod(scope: Scope, pod_id: uuid.UUID) -> IssuedResource:
    res = scope.require_issued_resource(pod_id)  # 404 outside scope
    if res.provider != Provider.runpod:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pod not found")
    return res


def _require_control(scope: Scope, pod_id: uuid.UUID) -> _Ctx:
    """The pod's owner, or the group's professor (403 for the department, D21)."""
    res = _require_pod(scope, pod_id)
    if res.user_id == scope.me.id:
        return _Ctx(res, True)
    scope.require_manage_group(res.group_id)
    return _Ctx(res, False)


def _state(res: IssuedResource) -> PodState:
    if res.status == ResourceStatus.terminated:
        return "terminated"
    if res.status == ResourceStatus.stopped:
        return "stopped"
    return "running" if res.details.get("ready") else "starting"


def _live_state(live: PodStatus) -> PodState:
    if live.status == "RUNNING":
        return "running" if live.ready else "starting"
    return "stopped"


def _spent_this_month(session: Session, res: IssuedResource) -> Decimal:
    total = session.exec(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.resource_id == res.id,
            LedgerEntry.type == LedgerType.spend,
            LedgerEntry.period == period_of(utcnow()),
        )
    ).one()
    return -Decimal(total)


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


def _pod_out(session: Session, res: IssuedResource, state: PodState | None = None) -> PodOut:
    d = res.details
    user = session.get(User, res.user_id)
    started = d.get("started_at")
    return PodOut(
        id=res.id,
        external_ref=res.external_ref,
        user_id=res.user_id,
        user_name=user.name if user else "",
        group_id=res.group_id,
        gpu_name=d.get("gpu_name") or d.get("gpu_type") or d.get("gpu_type_id") or "",
        cloud=d.get("cloud", "community"),
        price_per_hour=quantize(_dec(d.get("cost_per_hr"))),
        storage_per_hour=quantize(_dec(d.get("storage_cost_per_hr"))),
        container_disk_gb=int(d.get("container_disk_gb", 0)),
        volume_gb=int(d.get("volume_gb", d.get("disk_gb", 0))),
        state=state or _state(res),
        started_at=datetime.fromisoformat(started) if started and res.status == ResourceStatus.running else None,
        stopped_by=d.get("stopped_by") if res.status == ResourceStatus.stopped else None,
        spent_this_month_usd=quantize(_spent_this_month(session, res)),
        created_at=res.created_at,
    )


def _adapter_for(session: Session, res: IssuedResource) -> Any:
    adapter = adapter_for_credential(session, res.credential_id)
    if adapter is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The RunPod credential this pod was launched with has been revoked.",
        )
    return adapter


def _hourly(res: IssuedResource) -> Decimal:
    d = res.details
    return _dec(d.get("cost_per_hr")) + _dec(d.get("running_storage_per_hr"))


def _set(res: IssuedResource, **changes: Any) -> None:
    """details is JSONB: assign a new dict so SQLAlchemy sees the change."""
    d = dict(res.details)
    for k, v in changes.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    res.details = d


def _gone(session: Session, scope: Scope, res: IssuedResource, action: str) -> HTTPException:
    """RunPod no longer knows the pod: record it as terminated, then report."""
    res.status = ResourceStatus.terminated
    _set(res, ready=None)
    session.add(res)
    write_audit(session, scope.me, "pod.gone", "issued_resource", res.id, {"during": action})
    session.commit()
    return HTTPException(status.HTTP_409_CONFLICT, "This pod no longer exists on RunPod.")


# ====================================================================== endpoints
@router.get("/pods", response_model=list[PodOut])
def list_pods(scope: ScopeDep, group_id: uuid.UUID | None = None, include_terminated: bool = False) -> list[PodOut]:
    q = scope.issued_resources().where(IssuedResource.provider == Provider.runpod)
    if group_id is not None:
        q = q.where(IssuedResource.group_id == group_id)
    if not include_terminated:
        q = q.where(IssuedResource.status != ResourceStatus.terminated)
    q = q.order_by(IssuedResource.created_at.desc())  # type: ignore[attr-defined]
    return [_pod_out(scope.session, r) for r in scope.session.exec(q).all()]


@router.post("/pods", response_model=PodOut, status_code=status.HTTP_201_CREATED)
def launch_pod(body: PodLaunchIn, scope: ScopeDep) -> Any:
    session, me = scope.session, scope.me
    group = scope.require_group(body.group_id)  # 404 outside scope
    require_approved_access(session, me.id, group.id, Provider.runpod)  # 403: not approved or suspended
    cred = resolve_credential(session, group.professor_id, Provider.runpod)  # 409 if none
    adapter = get_adapter(Provider.runpod, cred.secret)

    try:
        offer = next((o for o in _gpus(adapter, cred.credential_id, body.cloud) if o.gpu_type_id == body.gpu_type_id), None)
        if offer is None:
            raise HTTPException(422, f"{body.gpu_type_id} is not offered on {body.cloud} cloud")
        if offer.availability == "none":
            raise LaunchRefused("unavailable", f"No {offer.name} is available on {body.cloud} cloud right now.")
        rates = hourly_rates(offer.price_per_hour, body.gpu_count, body.container_disk_gb, body.volume_gb)
        prelaunch_check(session, group.id, me.id, rates.running)

        spec = {
            "gpu_type_id": body.gpu_type_id, "cloud": body.cloud, "gpu_count": body.gpu_count,
            "container_disk_gb": body.container_disk_gb, "volume_gb": body.volume_gb, "jupyter": body.jupyter,
        }
        name = f"cw-{str(group.id)[:8]}-{str(me.id)[:8]}"  # D17
        issued = adapter.issue(IssueRequest(name=name, user_id=str(me.id), group_id=str(group.id), spec=spec))
    except LaunchRefused as refused:
        return refused.response()
    except NoCapacity:
        return LaunchRefused("no_capacity", "RunPod has no GPU of this type free right now. Try another GPU or cloud.").response()
    except InsufficientBalance:
        return LaunchRefused("budget", "The RunPod account behind this group's credential has no credit left. Tell your professor.").response()
    except AdapterError as exc:
        raise _provider_error(exc) from None

    now = utcnow()
    res = IssuedResource(
        user_id=me.id,
        group_id=group.id,
        provider=Provider.runpod,
        credential_id=cred.credential_id,
        external_ref=issued.external_ref,
        status=ResourceStatus.running,
        details={
            **{k: v for k, v in issued.details.items() if v is not None and k != "image"},
            "gpu_type_id": body.gpu_type_id,
            "gpu_name": offer.name,
            "cloud": body.cloud,
            "gpu_count": body.gpu_count,
            "container_disk_gb": body.container_disk_gb,
            "volume_gb": body.volume_gb,
            "jupyter": body.jupyter,
            "cost_per_hr": str(rates.gpu),
            "running_storage_per_hr": str(rates.running_storage),
            "storage_cost_per_hr": str(rates.stopped_storage),
            "started_at": now.isoformat(),
        },
    )
    session.add(res)
    try:
        session.flush()
        write_audit(
            session, me, "pod.launch", "issued_resource", res.id,
            {"group_id": group.id, "external_ref": res.external_ref, "gpu": offer.name, "cloud": body.cloud,
             "cost_per_hr": rates.gpu, "credential_id": cred.credential_id, "billable": cred.is_department},
        )
        session.commit()
    except Exception:
        # Don't leave a running pod nobody tracks.
        session.rollback()
        try:
            adapter.revoke(issued.external_ref)
        except AdapterError:
            log.error("Pod %s launched but not recorded, and terminating it failed", issued.external_ref)
        raise
    session.refresh(res)
    return _pod_out(session, res, "starting")


@router.get("/pods/{pod_id}", response_model=PodDetailOut)
def get_pod(pod_id: uuid.UUID, scope: ScopeDep) -> PodDetailOut:
    """Live state from RunPod. Connection details only while running; the Jupyter password
    only to the pod's owner. If RunPod can't be reached, the stored state is shown."""
    session = scope.session
    res = _require_pod(scope, pod_id)
    live: PodStatus | None = None
    state: PodState = _state(res)
    if res.status != ResourceStatus.terminated:
        adapter = adapter_for_credential(session, res.credential_id)
        try:
            live = adapter.pod_status(res.external_ref) if adapter is not None else None
        except ResourceNotFound:
            state = "terminated"  # the sync job records it
        except AdapterError as exc:
            log.warning("RunPod status for %s unavailable: %s", res.id, type(exc).__name__)
        if live is not None:
            state = _live_state(live)
            if live.ready and not res.details.get("ready") and res.status == ResourceStatus.running:
                started = live.started_at.isoformat() if live.started_at else res.details.get("started_at")
                _set(res, ready=True, started_at=started)
                session.add(res)
                session.commit()
                session.refresh(res)

    connection = None
    if live is not None and state == "running":
        connection = ConnectionOut(
            ssh_proxy=live.ssh_proxy,
            ssh_direct=live.ssh_direct,
            jupyter_url=live.jupyter_url,
            jupyter_password=live.jupyter_password if res.user_id == scope.me.id else None,
        )
    base = _pod_out(session, res, state)
    return PodDetailOut(
        **base.model_dump(),
        connection=connection,
        gpu_util_percent=live.gpu_util_percent if live is not None and state == "running" else None,
    )


@router.post("/pods/{pod_id}/stop", response_model=PodOut)
def stop_pod(pod_id: uuid.UUID, scope: ScopeDep) -> PodOut:
    session = scope.session
    ctx = _require_control(scope, pod_id)
    res = ctx.res
    if res.status != ResourceStatus.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "This pod is not running")
    adapter = _adapter_for(session, res)
    try:
        adapter.stop(res.external_ref)
    except ResourceNotFound:
        raise _gone(session, scope, res, "stop") from None
    except AdapterError as exc:
        raise _provider_error(exc) from None

    by = "owner" if ctx.as_owner else "professor"
    res.status = ResourceStatus.stopped
    _set(res, ready=None, stopped_by=by)
    session.add(res)
    write_audit(
        session, scope.me, "pod.stop", "issued_resource", res.id,
        {"group_id": res.group_id, "user_id": res.user_id, "by": by},
    )
    session.commit()
    session.refresh(res)
    return _pod_out(session, res)


@router.post("/pods/{pod_id}/start", response_model=PodOut)
def start_pod(pod_id: uuid.UUID, scope: ScopeDep) -> Any:
    """Resume a stopped pod, with the same 1-hour budget check. The owner also needs his
    RunPod access still approved and not to be suspended."""
    session = scope.session
    ctx = _require_control(scope, pod_id)
    res = ctx.res
    if res.status != ResourceStatus.stopped:
        return LaunchRefused("not_stopped", "Only a stopped pod can be started.").response()
    if ctx.as_owner:
        require_approved_access(session, scope.me.id, res.group_id, Provider.runpod)
    adapter = _adapter_for(session, res)
    try:
        prelaunch_check(session, res.group_id, res.user_id, _hourly(res))
        adapter.resume(res.external_ref)
    except LaunchRefused as refused:
        return refused.response()
    except NoCapacity:
        return LaunchRefused(
            "no_capacity",
            "The machine this pod lives on has no GPU free right now. Try again later.",
        ).response()
    except InsufficientBalance:
        return LaunchRefused("budget", "The RunPod account behind this pod has no credit left.").response()
    except ResourceNotFound:
        raise _gone(session, scope, res, "start") from None
    except AdapterError as exc:
        raise _provider_error(exc) from None

    by = "owner" if ctx.as_owner else "professor"
    res.status = ResourceStatus.running
    _set(res, ready=None, stopped_by=None, started_at=utcnow().isoformat())
    session.add(res)
    write_audit(
        session, scope.me, "pod.start", "issued_resource", res.id,
        {"group_id": res.group_id, "user_id": res.user_id, "by": by},
    )
    session.commit()
    session.refresh(res)
    return _pod_out(session, res, "starting")


@router.delete("/pods/{pod_id}", status_code=status.HTTP_204_NO_CONTENT)
def terminate_pod(pod_id: uuid.UUID, scope: ScopeDep) -> Response:
    """Delete the pod and its volume for good. Only a person does this, never the watchdog (D3)."""
    session = scope.session
    ctx = _require_control(scope, pod_id)
    res = ctx.res
    if res.status == ResourceStatus.terminated:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    adapter = _adapter_for(session, res)
    try:
        adapter.revoke(res.external_ref)  # already gone is fine
    except AdapterError as exc:
        raise _provider_error(exc) from None

    res.status = ResourceStatus.terminated
    _set(res, ready=None)
    session.add(res)
    write_audit(
        session, scope.me, "pod.terminate", "issued_resource", res.id,
        {"group_id": res.group_id, "user_id": res.user_id, "by": "owner" if ctx.as_owner else "professor"},
    )
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["LaunchRefused", "available_budget", "prelaunch_check", "router"]
