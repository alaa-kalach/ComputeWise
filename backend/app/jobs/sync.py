"""The sync job: pull usage from the providers, record it as spend, then enforce budgets.

    POST /jobs/sync    pg_cron every 5 minutes (D4), or "Sync now"

1. Usage. For every live key and pod, grouped by the credential it was issued under:
     - ask that credential's adapter for the TOTAL spent so far (fetch_usage)
     - RunPod only reports what it has BILLED, and billing lags (docs/notes/runpod.md), so
       for pods the total is our own estimate when that is higher (D15):
           last total + hours since then x the pod's rate
       rate = GPU price + running storage while running, volume storage while stopped.
       That is how a stopped pod's disk shows up in the team's spend.
     - new spend = this total - the last snapshot's total (usage_delta: never negative)
     - save a usage_snapshot, and a spend line with the provider and the credential_id that
       paid, so reports can split billable from observed (D8)
     - if RunPod stopped or started a pod on its own, record its new state
2. Revocations. Keys and pods still live under a revoked credential or grant are cut off
   (watchdog.sweep_revoked), in case the revoke hook found the provider down.
3. Budgets. Warn at 80%, stop pods and cap keys at 100%, recalculate every key's limit
   (watchdog.enforce_budgets).

Calling it twice in a row writes spend once: the second run finds no difference (a pod's
estimate only grows with the time between runs). A database lock stops two runs from
overlapping. A provider that is down is skipped and retried next run; one failing
credential never stops the others.

Not done here: correcting the estimate DOWN when RunPod's bill turns out lower (D15's
reconciliation). The total never drops below the bill, so the ledger never under-counts.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlmodel import Session, select

from app.access.credentials import adapter_for_credential
from app.adapters import AdapterError, ResourceState
from app.budget.ledger import ZERO, money, period_of, spend_entry, usage_delta
from app.core.audit import write_audit
from app.jobs.watchdog import Lookup, WatchdogResult, enforce_budgets, last_snapshot, sweep_revoked
from app.models import IssuedResource, Provider, ResourceStatus, UsageSnapshot
from app.models.base import utcnow

log = logging.getLogger(__name__)

SYNC_LOCK_ID = 7301001  # any fixed number: every sync run asks for the same lock
LIVE = (ResourceStatus.active, ResourceStatus.capped, ResourceStatus.running, ResourceStatus.stopped)


@dataclass
class SyncResult:
    resources_polled: int = 0
    spend_entries: int = 0
    skipped: bool = False  # another run held the lock
    errors: list[str] = field(default_factory=list)
    watchdog: WatchdogResult = field(default_factory=WatchdogResult)


def _rate(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def pod_hourly_rate(res: IssuedResource) -> Decimal:
    """What the pod costs per hour in its CURRENT state (our record, from launch time)."""
    d = res.details or {}
    if res.status == ResourceStatus.running:
        return _rate(d.get("cost_per_hr")) + _rate(d.get("running_storage_per_hr"))
    return _rate(d.get("storage_cost_per_hr"))


def estimate_pod_total(res: IssuedResource, last: UsageSnapshot | None, now: datetime) -> Decimal:
    """Last total + hours since then x the pod's rate. With no snapshot yet, from launch."""
    since = last.polled_at if last else res.created_at
    base = last.cumulative_usd if last else ZERO
    seconds = max(Decimal(0), Decimal(str((now - since).total_seconds())))
    return money(base + pod_hourly_rate(res) * seconds / Decimal(3600))


def _follow_pod_state(session: Session, res: IssuedResource, state: ResourceState, now: datetime) -> None:
    """RunPod stopped or started a pod without us (out of credit, the RunPod console...)."""
    if res.status == ResourceStatus.running and state is ResourceState.disabled:
        res.status = ResourceStatus.stopped
        res.details = {**{k: v for k, v in (res.details or {}).items() if k != "ready"}, "stopped_by": "provider"}
    elif res.status == ResourceStatus.stopped and state is ResourceState.active:
        res.status = ResourceStatus.running
        res.details = {**{k: v for k, v in (res.details or {}).items() if k != "stopped_by"},
                       "started_at": now.isoformat()}
    else:
        return
    session.add(res)
    write_audit(session, None, "pod.state_sync", "issued_resource", res.id,
                {"group_id": res.group_id, "now": res.status, "by": "provider"})


def run_sync(session: Session, now: datetime | None = None) -> SyncResult:
    """One sync run. Commits its own work."""
    result = SyncResult()
    # Held until the commit at the end; a second run arriving meanwhile gets False and stops.
    if not session.execute(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": SYNC_LOCK_ID}).scalar():
        result.skipped = True
        return result

    now = now or utcnow()
    period = period_of(now)

    # ---------------------------------------------------------------- 1. usage -> spend
    resources = session.exec(select(IssuedResource).where(IssuedResource.status.in_(LIVE))).all()  # type: ignore[attr-defined]
    by_credential: dict[uuid.UUID, list[IssuedResource]] = defaultdict(list)
    for r in resources:
        by_credential[r.credential_id].append(r)

    for credential_id, group in by_credential.items():
        adapter = adapter_for_credential(session, credential_id)
        if adapter is None:  # revoked credential: step 2 cuts its keys and pods off
            continue
        try:
            readings = adapter.fetch_usage([r.external_ref for r in group])
        except AdapterError as e:
            result.errors.append(f"credential {credential_id}: {type(e).__name__}")
            log.warning("sync: skipped credential %s (%s)", credential_id, type(e).__name__)
            continue

        for r in group:
            reading = readings.get(r.external_ref)
            if reading is None or reading.state is ResourceState.gone:
                continue  # unknown at the provider: no snapshot, so it never restarts from 0
            result.resources_polled += 1
            last = last_snapshot(session, r.id)
            total = reading.cumulative_usd
            if r.provider is Provider.runpod and adapter.usage_lags:
                total = max(total, estimate_pod_total(r, last, now))
            delta = usage_delta(last.cumulative_usd if last else None, total)
            session.add(UsageSnapshot(issued_resource_id=r.id, polled_at=now, cumulative_usd=total))
            if delta > ZERO:
                session.add(spend_entry(
                    r.user_id, r.group_id, period, delta,
                    provider=r.provider, credential_id=r.credential_id, resource_id=r.id,
                ))
                result.spend_entries += 1
            if r.provider is Provider.runpod:
                _follow_pod_state(session, r, reading.state, now)

    session.flush()

    # ---------------------------------------------------------------- 2 and 3. the watchdog
    lookup = Lookup(session)
    sweep_revoked(session, lookup, result.watchdog)
    enforce_budgets(session, period, lookup, result.watchdog)
    result.errors.extend(result.watchdog.errors)

    session.commit()
    return result