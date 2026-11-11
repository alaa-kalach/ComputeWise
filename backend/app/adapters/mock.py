"""Fake OpenRouter and RunPod adapters with spend you control. No network, no money.

Everyone builds against these until the real adapters exist:

    ad = MockOpenRouterAdapter("any-secret")
    key = ad.issue(IssueRequest(name="sara", user_id="u1", group_id="g1", limit_usd=Decimal("20")))
    WORLD.spend(key.external_ref, Decimal("5"))      # "spend $5 now"
    ad.fetch_usage([key.external_ref])                # total is now 5

    pods = MockRunPodAdapter("any-secret")
    pod = pods.issue(IssueRequest(name="omar", user_id="u2", group_id="g1", spec={"gpu_type": "RTX A4000"}))
    WORLD.advance(hours=10)                           # running pod: 10 h x $0.17

They copy the real providers where it matters:
- An OpenRouter key stops spending at its limit, and a disabled key can't spend.
- A running pod costs its GPU price per hour, a stopped pod costs disk storage per hour,
  and a terminated pod costs nothing. RunPod has no per-pod limit.
- WORLD.outage = True makes every call fail, to test what happens when a provider is down.

Try it:  python -m app.adapters.mock
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.adapters.base import (
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

GPU_PRICES: dict[str, Decimal] = {
    "CPU": Decimal("0.06"),
    "RTX A4000": Decimal("0.17"),
    "RTX A5000": Decimal("0.27"),
    "RTX 4090": Decimal("0.69"),
    "A100 PCIe": Decimal("1.64"),
    "H100 PCIe": Decimal("2.39"),
}
DEFAULT_GPU = "RTX A4000"
STORAGE_PER_GB_MONTH = Decimal("0.10")
HOURS_PER_MONTH = Decimal("730")
CENT4 = Decimal("0.0001")


def _round(x: Decimal) -> Decimal:
    return x.quantize(CENT4)


@dataclass
class MockResource:
    """One fake key or pod, as the provider would see it."""

    provider: Provider
    name: str
    user_id: str
    group_id: str
    state: ResourceState = ResourceState.active
    total_usd: Decimal = Decimal("0")
    limit_usd: Decimal | None = None
    cost_per_hr: Decimal = Decimal("0")
    storage_cost_per_hr: Decimal = Decimal("0")


@dataclass
class MockWorld:
    """The fake provider side: every key and pod, a clock, and an outage switch."""

    resources: dict[str, MockResource] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    outage: bool = False

    def spend(self, external_ref: str, usd: Decimal | int | str) -> Decimal:
        """Generate usage now. Returns what was actually charged: a key can't go past its
        limit or spend while disabled, and something gone charges nothing."""
        r = self.get(external_ref)
        amount = Decimal(str(usd))
        if r.state is not ResourceState.active:
            return Decimal("0")
        if r.provider is Provider.openrouter and r.limit_usd is not None:
            amount = max(Decimal("0"), min(amount, r.limit_usd - r.total_usd))
        r.total_usd = _round(r.total_usd + amount)
        return _round(amount)

    def advance(self, *, hours: float = 0, minutes: float = 0) -> None:
        """Move the clock forward: running pods pay GPU time, stopped pods pay storage."""
        delta = Decimal(str(hours)) + Decimal(str(minutes)) / 60
        for r in self.resources.values():
            if r.provider is not Provider.runpod:
                continue
            if r.state is ResourceState.active:
                rate = r.cost_per_hr
            elif r.state is ResourceState.disabled:
                rate = r.storage_cost_per_hr
            else:
                continue
            r.total_usd = _round(r.total_usd + rate * delta)
        self.now += timedelta(hours=float(delta))

    def reset(self) -> None:
        self.resources.clear()
        self.outage = False

    def get(self, external_ref: str) -> MockResource:
        try:
            return self.resources[external_ref]
        except KeyError:
            raise ResourceNotFound(external_ref) from None

    def check(self) -> None:
        if self.outage:
            raise ProviderUnavailable("mock outage")


WORLD = MockWorld()


class _MockBase(ProviderAdapter):
    """What both fake providers have in common."""

    def __init__(self, secret: str, world: MockWorld | None = None) -> None:
        if not secret:
            raise ValueError("an adapter needs its credential's secret")
        self._world = world or WORLD

    def _mine(self, ref: str) -> MockResource:
        r = self._world.get(ref)
        if r.provider is not self.provider:
            raise ResourceNotFound(ref)
        return r

    def fetch_usage(self, external_refs: list[str]) -> dict[str, UsageReading]:
        self._world.check()
        out: dict[str, UsageReading] = {}
        for ref in external_refs:
            r = self._world.resources.get(ref)
            if r is None or r.provider is not self.provider:
                out[ref] = UsageReading(ref, Decimal("0"), ResourceState.gone)
            else:
                out[ref] = UsageReading(ref, r.total_usd, r.state, r.limit_usd)
        return out

    def revoke(self, external_ref: str) -> None:
        self._world.check()
        r = self._world.resources.get(external_ref)
        if r is not None and r.provider is self.provider:
            r.state = ResourceState.gone

    def _set_state(self, ref: str, state: ResourceState) -> None:
        self._world.check()
        r = self._mine(ref)
        if r.state is ResourceState.gone:
            raise ResourceNotFound(ref)
        r.state = state

    def stop(self, external_ref: str) -> None:
        self._set_state(external_ref, ResourceState.disabled)

    def resume(self, external_ref: str) -> None:
        self._set_state(external_ref, ResourceState.active)


class MockOpenRouterAdapter(_MockBase):
    provider = Provider.openrouter
    supports_limits = True

    def issue(self, request: IssueRequest) -> IssuedAccess:
        self._world.check()
        key = "sk-or-v1-mock-" + secrets.token_hex(16)
        ref = hashlib.sha256(key.encode()).hexdigest()
        self._world.resources[ref] = MockResource(
            provider=self.provider,
            name=request.name,
            user_id=str(request.user_id),
            group_id=str(request.group_id),
            limit_usd=request.limit_usd,
        )
        return IssuedAccess(external_ref=ref, secret=key, details={"name": request.name})

    def set_limit(self, external_ref: str, limit_usd: Decimal) -> None:
        self._world.check()
        self._mine(external_ref).limit_usd = _round(Decimal(limit_usd))


class MockRunPodAdapter(_MockBase):
    provider = Provider.runpod
    supports_limits = False

    def estimate_hourly_cost(self, spec: dict[str, Any]) -> Decimal:
        gpu = spec.get("gpu_type", DEFAULT_GPU)
        count = int(spec.get("gpu_count", 1))
        return GPU_PRICES.get(gpu, GPU_PRICES[DEFAULT_GPU]) * count

    def issue(self, request: IssueRequest) -> IssuedAccess:
        self._world.check()
        spec = request.spec
        gpu = spec.get("gpu_type", DEFAULT_GPU)
        disk_gb = int(spec.get("disk_gb", 20))
        cost = self.estimate_hourly_cost(spec)
        storage = _round(disk_gb * STORAGE_PER_GB_MONTH / HOURS_PER_MONTH)
        ref = "mockpod-" + secrets.token_hex(6)
        self._world.resources[ref] = MockResource(
            provider=self.provider,
            name=request.name,
            user_id=str(request.user_id),
            group_id=str(request.group_id),
            cost_per_hr=cost,
            storage_cost_per_hr=storage,
        )
        return IssuedAccess(
            external_ref=ref,
            connection={
                "ssh": f"ssh root@{ref}.mock.runpod.net -p 22",
                "jupyter_url": f"https://{ref}-8888.proxy.runpod.net",
            },
            details={
                "gpu_type": gpu,
                "gpu_count": int(spec.get("gpu_count", 1)),
                "cost_per_hr": str(cost),
                "disk_gb": disk_gb,
                "storage_cost_per_hr": str(storage),
            },
        )

    def set_limit(self, external_ref: str, limit_usd: Decimal) -> None:
        raise NotSupported("runpod has no per-pod spend limit; the watchdog stops pods instead")


def _demo() -> None:
    """Issue a fake key, spend on it, read it back; then run and stop a fake pod."""
    from app.adapters.mock import WORLD, MockOpenRouterAdapter, MockRunPodAdapter

    keys = MockOpenRouterAdapter("demo-secret")
    key = keys.issue(IssueRequest(name="sara", user_id="sara", group_id="fyp1", limit_usd=Decimal("20")))
    print(f"issued key (shown once): {key.secret}")
    print(f"charged ${WORLD.spend(key.external_ref, 5)}, total now {keys.fetch_usage([key.external_ref])[key.external_ref].cumulative_usd}")
    print(f"tried to spend $30 more, charged ${WORLD.spend(key.external_ref, 30)} (the $20 limit stops it)")

    pods = MockRunPodAdapter("demo-secret")
    pod = pods.issue(IssueRequest(name="omar", user_id="omar", group_id="fyp1", spec={"gpu_type": "A100 PCIe"}))
    WORLD.advance(hours=2)
    pods.stop(pod.external_ref)
    WORLD.advance(hours=24)
    total = pods.fetch_usage([pod.external_ref])[pod.external_ref].cumulative_usd
    print(f"pod: 2 h on an A100 + 24 h stopped (disk only) = ${total}")


if __name__ == "__main__":
    _demo()