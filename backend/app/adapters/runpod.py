"""RunPod adapter (B4), on REST API v2 only (v1 retires 2026-11-15, D14).

Implements the shared ProviderAdapter interface (base.py) plus three RunPod-only calls the
pod endpoints need:

    list_gpus(cloud)      the GPU catalog with live price and availability (pre-launch check)
    pod_status(ref)       live state, connection details, GPU use (GET /pods/{id})
    hourly_rates(...)     GPU + storage cost per hour for a configuration

Everything here follows docs/notes/runpod.md, recorded against the live API:
  * status RUNNING does not mean ready: the pod is up when `runtime` is non-null.
  * terminate returns 204 and the pod then 404s for good (state "gone").
  * billing lags (minutes to hours), so fetch_usage's cumulative_usd is the BILLED total and
    trails reality; the sync job estimates live spend as cost x running time (D15).
  * JUPYTER_PASSWORD is in the pod env. It is returned by pod_status for the owner only and
    never stored or logged.

Plain HTTP and Decimal money, no database, no audit (the caller does both).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.adapters.base import (
    AdapterError,
    IssuedAccess,
    IssueRequest,
    NotSupported,
    ProviderAdapter,
    ProviderUnavailable,
    ResourceNotFound,
    ResourceState,
    UsageReading,
)
from app.models import Provider

BASE_URL = "https://api.runpod.io/v2"
DEFAULT_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"  # the probe launched this as-is
CLOUDS = ("community", "secure")

# Storage, from RunPod's billing docs (notes, "Billing and storage"). Per GB per month.
CONTAINER_DISK_RUNNING = Decimal("0.10")  # wiped on stop, so only billed while running
VOLUME_RUNNING = Decimal("0.10")
VOLUME_STOPPED = Decimal("0.20")
HOURS_PER_MONTH = Decimal("730")
CENT4 = Decimal("0.0001")
# Billing window read by fetch_usage (daily buckets). Pods older than this undercount; the
# project runs one month, so 31 days is enough. The probe used lastN 3 and 24.
BILLING_DAYS = 31


class NoCapacity(AdapterError):
    """RunPod has no GPU free for this request (create, or start on the same machine)."""


class InsufficientBalance(AdapterError):
    """The RunPod account behind the credential has no credit left (402)."""


def _money(x: Any) -> Decimal:
    return Decimal(str(x or 0)).quantize(CENT4, rounding=ROUND_HALF_UP)


def _rate(gb: int, per_gb_month: Decimal) -> Decimal:
    return (Decimal(gb) * per_gb_month / HOURS_PER_MONTH).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class GpuOffer:
    gpu_type_id: str  # RunPod's id, e.g. "NVIDIA RTX A2000"; this is what POST /pods takes
    name: str  # short name, e.g. "RTX A2000"
    vram_gb: int  # the catalog's `memory` (VRAM). Not the pod's gpu.memory, which is host RAM.
    cloud: str  # "community" | "secure"
    price_per_hour: Decimal  # per GPU
    availability: str  # "none" | "low" | "medium" | "high"
    max_count: int


@dataclass(frozen=True)
class HourlyRates:
    gpu: Decimal  # price x count
    running_storage: Decimal  # container disk + volume while running
    stopped_storage: Decimal  # volume only, while stopped

    @property
    def running(self) -> Decimal:
        return self.gpu + self.running_storage


def hourly_rates(price_per_gpu: Decimal, gpu_count: int, container_disk_gb: int, volume_gb: int) -> HourlyRates:
    return HourlyRates(
        gpu=Decimal(price_per_gpu) * gpu_count,
        running_storage=_rate(container_disk_gb, CONTAINER_DISK_RUNNING) + _rate(volume_gb, VOLUME_RUNNING),
        stopped_storage=_rate(volume_gb, VOLUME_STOPPED),
    )


@dataclass(frozen=True)
class PodStatus:
    external_ref: str
    status: str  # RunPod's own: "RUNNING" | "EXITED" | ...
    ready: bool  # runtime is non-null: SSH and ports work
    started_at: datetime | None  # start of the current session
    cost_per_hr: Decimal | None
    ssh_proxy: str | None = None
    ssh_direct: str | None = None
    jupyter_url: str | None = None
    jupyter_password: str | None = field(default=None, repr=False)  # owner only; never store or log
    gpu_util_percent: int | None = None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_pod(pod: dict[str, Any]) -> PodStatus:
    """A RunPod pod object (create, GET, stop/start responses) -> PodStatus."""
    runtime = pod.get("runtime")
    ready = pod.get("status") == "RUNNING" and runtime is not None
    ssh = pod.get("ssh") or {}
    proxy = (ssh.get("proxy") or {}).get("command")
    direct = (ssh.get("direct") or {}).get("command") if ready else None
    jupyter = any(str(p).startswith("8888/") for p in pod.get("ports") or [])
    gpus = (runtime or {}).get("gpus") or []
    util = gpus[0].get("util") if gpus and isinstance(gpus[0], dict) else None
    return PodStatus(
        external_ref=pod["id"],
        status=pod.get("status", ""),
        ready=ready,
        started_at=_parse_time(pod.get("startedAt")),
        cost_per_hr=_money(pod["cost"]) if pod.get("cost") is not None else None,
        ssh_proxy=proxy,
        ssh_direct=direct,
        # HTTP ports go through RunPod's proxy (RunPod convention, not in the API response).
        jupyter_url=f"https://{pod['id']}-8888.proxy.runpod.net" if jupyter and ready else None,
        jupyter_password=(pod.get("env") or {}).get("JUPYTER_PASSWORD") if jupyter else None,
        gpu_util_percent=int(util) if util is not None else None,
    )


class RunPodAdapter(ProviderAdapter):
    provider = Provider.runpod
    supports_limits = False  # RunPod has no per-pod cap: the watchdog stops pods instead
    usage_lags = True  # fetch_usage reads billing, which lags: the sync estimates on top (D15)
    def __init__(self, secret: str, *, base_url: str = BASE_URL, client: httpx.Client | None = None):
        if not secret:
            raise ValueError("RunPod API key is missing")
        self._client = client or httpx.Client(timeout=30)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    # -- the only place that talks to the network, so errors never leak the key -----------
    def _call(self, method: str, path: str, *, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._client.request(method, self._base + path, headers=self._headers, json=body, params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"could not reach RunPod ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            message = f"RunPod {resp.status_code}: {detail}"
            if resp.status_code == 404:
                raise ResourceNotFound(message)
            if resp.status_code == 402:
                raise InsufficientBalance(message)
            if resp.status_code == 400:
                raise NoCapacity(message)  # documented: rule violation or no capacity
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(message)
            raise AdapterError(message)  # 401 bad key, 403 missing permission, 422 bad body
        return resp.json() if resp.content else {}

    def _action(self, ref: str, action: str) -> Any:
        return self._call("POST", f"/pods/{ref}/action", body={"action": action})

    # -- RunPod-only ---------------------------------------------------------------------
    def list_gpus(self, cloud: str) -> list[GpuOffer]:
        """GPUs offered on one cloud, with price and availability. Ones the cloud doesn't
        offer at all (price 0) are left out; availability "none" is kept (shown, not launchable)."""
        if cloud not in CLOUDS:
            raise ValueError(f"cloud must be one of {CLOUDS}")
        out = self._call(
            "GET", "/catalog/gpus", params={"include": "AVAILABILITY", "product": "POD", "cloud": cloud.upper()}
        )
        offers = []
        for g in out.get("gpus", []):
            price = (g.get("price") or {}).get(cloud)
            max_count = int((g.get("maxCount") or {}).get(cloud) or 0)
            if not price or not g.get(cloud, True) or max_count < 1:
                continue
            offers.append(
                GpuOffer(
                    gpu_type_id=g["id"],
                    name=g.get("name") or g["id"],
                    vram_gb=int(g.get("memory") or 0),
                    cloud=cloud,
                    price_per_hour=_money(price),
                    availability=str(g.get("availability") or "NONE").lower(),
                    max_count=max_count,
                )
            )
        return sorted(offers, key=lambda o: (o.price_per_hour, o.name))

    def pod_status(self, external_ref: str) -> PodStatus:
        """Live state from GET /pods/{id} (the list endpoint never has runtime)."""
        return parse_pod(self._call("GET", f"/pods/{external_ref}"))

    # -- ProviderAdapter -----------------------------------------------------------------
    def issue(self, request: IssueRequest) -> IssuedAccess:
        """Launch a pod. spec: gpu_type_id, cloud, gpu_count, container_disk_gb, volume_gb,
        jupyter, image (optional). The pre-launch budget check is the caller's job.

        Returns at once: RunPod says RUNNING, but connection details only exist once
        `runtime` appears, so `connection` is empty here; poll pod_status()."""
        spec = request.spec
        cloud = spec.get("cloud", "community")
        if cloud not in CLOUDS:
            raise ValueError(f"cloud must be one of {CLOUDS}")
        jupyter = bool(spec.get("jupyter", True))
        body = {
            "name": request.name,
            "image": spec.get("image", DEFAULT_IMAGE),
            "gpu": {"id": spec["gpu_type_id"], "count": int(spec.get("gpu_count", 1))},
            "cloud": cloud.upper(),
            "disk": int(spec.get("container_disk_gb", 10)),
            "mounts": {"persistent": {"size": int(spec.get("volume_gb", 10)), "path": "/workspace"}},
            "ports": ["8888/http", "22/tcp"] if jupyter else ["22/tcp"],
            "startJupyter": jupyter,
        }
        pod = self._call("POST", "/pods", body=body)
        status = parse_pod(pod)
        return IssuedAccess(
            external_ref=pod["id"],
            details={
                "name": request.name,
                "gpu_type_id": body["gpu"]["id"],
                "gpu_count": body["gpu"]["count"],
                "cloud": cloud,
                "container_disk_gb": body["disk"],
                "volume_gb": body["mounts"]["persistent"]["size"],
                "jupyter": jupyter,
                "image": body["image"],
                # RunPod's own per-hour GPU price for this pod, when it reports one.
                "runpod_cost_per_hr": str(status.cost_per_hr) if status.cost_per_hr is not None else None,
            },
        )

    def revoke(self, external_ref: str) -> None:
        """Terminate for good: the volume is deleted and RunPod forgets the pod. Only a person
        does this, never the watchdog (D3). Already gone is fine."""
        try:
            self._action(external_ref, "terminate")
        except ResourceNotFound:
            pass

    def stop(self, external_ref: str) -> None:
        """Release the GPU, keep the volume (still billed as storage)."""
        self._action(external_ref, "stop")

    def resume(self, external_ref: str) -> None:
        """Start a stopped pod on the same machine. NoCapacity if its GPU is taken meanwhile."""
        self._action(external_ref, "start")

    def fetch_usage(self, external_refs: list[str]) -> dict[str, UsageReading]:
        """BILLED total per pod over the last BILLING_DAYS days, and its state. Billing lags,
        so this trails live spend: use it to reconcile, not to enforce (D15). Unknown pods
        come back as gone."""
        if not external_refs:
            return {}
        billed: dict[str, Decimal] = {}
        out = self._call("GET", "/billing/pods", params={"bucketSize": "day", "lastN": BILLING_DAYS})
        for rec in out.get("records", []):
            ref = rec.get("podId")
            if ref in external_refs:
                billed[ref] = billed.get(ref, Decimal(0)) + Decimal(str(rec.get("totalAmount") or 0))

        readings: dict[str, UsageReading] = {}
        for ref in external_refs:
            total = _money(billed.get(ref, 0))
            try:
                status = self.pod_status(ref)
            except ResourceNotFound:
                readings[ref] = UsageReading(ref, total, ResourceState.gone)
                continue
            state = ResourceState.active if status.status == "RUNNING" else ResourceState.disabled
            readings[ref] = UsageReading(ref, total, state)
        return readings

    def estimate_hourly_cost(self, spec: dict[str, Any]) -> Decimal | None:
        """GPU + running storage per hour for a launch spec, from the live catalog."""
        cloud = spec.get("cloud", "community")
        offer = next((o for o in self.list_gpus(cloud) if o.gpu_type_id == spec.get("gpu_type_id")), None)
        if offer is None:
            return None
        return hourly_rates(
            offer.price_per_hour, int(spec.get("gpu_count", 1)),
            int(spec.get("container_disk_gb", 10)), int(spec.get("volume_gb", 10)),
        ).running

    def set_limit(self, external_ref: str, limit_usd: Decimal) -> None:
        raise NotSupported("runpod has no per-pod spend limit; the watchdog stops pods instead")


__all__ = [
    "BASE_URL",
    "GpuOffer",
    "HourlyRates",
    "InsufficientBalance",
    "NoCapacity",
    "PodStatus",
    "RunPodAdapter",
    "hourly_rates",
    "parse_pod",
]
