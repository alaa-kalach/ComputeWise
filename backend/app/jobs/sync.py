"""The sync job, version 1 (Week 2): pull usage from the providers, keep a snapshot of each
resource's total, and turn the difference into spend lines in the ledger.

    POST /jobs/sync    pg_cron every 5 minutes (D4), or "Sync now"

For every live key and pod, grouped by the credential it was issued under:
    1. ask that credential's adapter for the TOTAL spent so far (fetch_usage)
    2. new spend = this total - the last snapshot's total (usage_delta: never negative)
    3. save a usage_snapshot, and a spend line that records the provider and the
       credential_id that paid, so reports can split billable from observed (D8)

Calling it twice in a row writes spend once: the second run finds no difference.
A database lock stops two runs from overlapping. A provider that is down is skipped and
retried next run; one failing credential never stops the others.

Week 3 adds on top (B5, B6): recalculating key limits from the team balance, the 80% and
100% rules, and counting stopped pods' storage.
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
from app.budget.ledger import ZERO, period_of, spend_entry, usage_delta
from app.models import IssuedResource, ResourceStatus, UsageSnapshot
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


def _last_total(session: Session, resource_id: uuid.UUID) -> Decimal | None:
    return session.exec(
        select(UsageSnapshot.cumulative_usd)
        .where(UsageSnapshot.issued_resource_id == resource_id)
        .order_by(UsageSnapshot.polled_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()


def run_sync(session: Session, now: datetime | None = None) -> SyncResult:
    """One sync run. Commits its own work."""
    result = SyncResult()
    # Held until the commit at the end; a second run arriving meanwhile gets False and stops.
    if not session.execute(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": SYNC_LOCK_ID}).scalar():
        result.skipped = True
        return result

    now = now or utcnow()
    period = period_of(now)
    resources = session.exec(select(IssuedResource).where(IssuedResource.status.in_(LIVE))).all()  # type: ignore[attr-defined]
    by_credential: dict[uuid.UUID, list[IssuedResource]] = defaultdict(list)
    for r in resources:
        by_credential[r.credential_id].append(r)

    for credential_id, group in by_credential.items():
        adapter = adapter_for_credential(session, credential_id)
        if adapter is None:  # revoked credential: its keys and pods are the revoke cascade's job
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
            delta = usage_delta(_last_total(session, r.id), reading.cumulative_usd)
            session.add(UsageSnapshot(issued_resource_id=r.id, polled_at=now, cumulative_usd=reading.cumulative_usd))
            if delta > ZERO:
                session.add(spend_entry(
                    r.user_id, r.group_id, period, delta,
                    provider=r.provider, credential_id=r.credential_id, resource_id=r.id,
                ))
                result.spend_entries += 1

    session.commit()
    return result