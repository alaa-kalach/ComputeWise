"""The watchdog (B6): the budget rules, applied by the sync job right after it records spend.

    80%    the team gets one warning notification (level "warning")
    100%   running pods are STOPPED, never terminated (D3); OpenRouter keys are capped at what
           they have used (limit = usage); the team gets one "critical" notification
    always every live key's limit = its usage so far + what its user may still spend (the
           design's formula), so a top-up re-opens capped keys on the next sync
    revoked credential or grant: its keys are disabled and its running pods stopped, at once
           (the revoke hook below) and again on every sync for whatever the hook couldn't reach

Hard limits never wait for a human: this is plain code, not the agent. A user's own
sub-budget (D9) counts too: his pods stop when HE has nothing left, even if the team has.

Every action is written to the audit log with no actor (system) or, for a revocation, the
person who revoked. Nothing here commits: the sync job (or the revoke endpoint) does.
A provider that is down is skipped and retried on the next sync.

Notifications: one per team per threshold per month. A top-up or correction re-arms them,
so a team that is topped up and runs out again is told again.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from app.access.credentials import CredentialRevoked, on_credential_revoked
from app.adapters import AdapterError, ProviderAdapter, ResourceNotFound, get_adapter
from app.budget.ledger import (
    FUNDING,
    ZERO,
    BudgetStatus,
    Totals,
    available_to_user,
    budget_status,
    key_limit,
    team_totals,
    user_totals,
)
from app.core.audit import write_audit
from app.core.crypto import decrypt
from app.models import (
    AuditLog,
    BudgetPolicy,
    CredentialGrant,
    CredentialStatus,
    GrantStatus,
    Group,
    IssuedResource,
    LedgerEntry,
    Notification,
    OwnerType,
    Provider,
    ProviderCredential,
    ResourceStatus,
    ScopeType,
    UsageSnapshot,
)

if TYPE_CHECKING:
    from app.core.auth import CurrentUser

log = logging.getLogger(__name__)

# What the watchdog can still act on. Stopped pods are already cut off (only storage runs),
# disabled keys and terminated pods are done.
ACTIONABLE = (ResourceStatus.active, ResourceStatus.capped, ResourceStatus.running)

WARN_ACTION = "budget.warn"  # 80%
EXHAUSTED_ACTION = "budget.exhausted"  # 100%


@dataclass
class WatchdogResult:
    warnings: int = 0  # notifications sent (80% and 100%)
    pods_stopped: int = 0
    keys_capped: int = 0
    keys_reopened: int = 0
    limits_set: int = 0  # other limit changes
    cut_off: int = 0  # keys and pods under a revoked credential or grant
    errors: list[str] = field(default_factory=list)


# ====================================================================== small helpers
def last_snapshot(session: Session, resource_id: uuid.UUID) -> UsageSnapshot | None:
    return session.exec(
        select(UsageSnapshot)
        .where(UsageSnapshot.issued_resource_id == resource_id)
        .order_by(UsageSnapshot.polled_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()


def period_start(period: str) -> datetime:
    return datetime(int(period[:4]), int(period[5:7]), 1, tzinfo=timezone.utc)


def _set_details(res: IssuedResource, **changes: Any) -> None:
    """details is JSONB: assign a new dict so SQLAlchemy sees the change. None removes a key."""
    d = dict(res.details or {})
    for k, v in changes.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    res.details = d


def _usd(x: Decimal) -> str:
    return f"${x:,.2f}"


class Lookup:
    """Per-run caches: one adapter per credential, and whether a credential is cut off."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._adapters: dict[uuid.UUID, ProviderAdapter | None] = {}
        self._professor_of: dict[uuid.UUID, uuid.UUID | None] = {}
        self._reasons: dict[tuple[uuid.UUID, uuid.UUID | None], str | None] = {}

    def adapter(self, credential_id: uuid.UUID) -> ProviderAdapter | None:
        """The adapter for the credential a resource was issued under, EVEN IF it was revoked:
        cutting off a revoked credential's keys and pods still needs its secret."""
        if credential_id not in self._adapters:
            cred = self.session.get(ProviderCredential, credential_id)
            self._adapters[credential_id] = (
                None if cred is None else get_adapter(cred.provider, decrypt(cred.secret_encrypted))
            )
        return self._adapters[credential_id]

    def professor_of(self, group_id: uuid.UUID) -> uuid.UUID | None:
        if group_id not in self._professor_of:
            group = self.session.get(Group, group_id)
            self._professor_of[group_id] = group.professor_id if group else None
        return self._professor_of[group_id]

    def cut_off_reason(self, res: IssuedResource) -> str | None:
        """'credential.revoke' or 'grant.revoke' if the money behind this resource is gone."""
        professor_id = self.professor_of(res.group_id)
        key = (res.credential_id, professor_id)
        if key not in self._reasons:
            self._reasons[key] = self._reason(res.credential_id, professor_id)
        return self._reasons[key]

    def _reason(self, credential_id: uuid.UUID, professor_id: uuid.UUID | None) -> str | None:
        cred = self.session.get(ProviderCredential, credential_id)
        if cred is None or cred.status != CredentialStatus.active:
            return "credential.revoke"
        if cred.owner_type == OwnerType.department:
            grant = self.session.exec(
                select(CredentialGrant.id).where(
                    CredentialGrant.credential_id == credential_id,
                    CredentialGrant.professor_id == professor_id,
                    CredentialGrant.status == GrantStatus.active,
                )
            ).first()
            if grant is None:
                return "grant.revoke"
        return None


# ====================================================================== acting on one resource
def stop_pod(
    session: Session, res: IssuedResource, adapter: ProviderAdapter, *, by: str, reason: str,
    actor: CurrentUser | None = None,
) -> None:
    """Stop a running pod (volume kept, D3) and record it. Raises AdapterError if RunPod
    can't be reached or refuses; nothing is changed then."""
    adapter.stop(res.external_ref)
    res.status = ResourceStatus.stopped
    _set_details(res, ready=None, stopped_by=by)
    session.add(res)
    write_audit(
        session, actor, "pod.stop", "issued_resource", res.id,
        {"group_id": res.group_id, "user_id": res.user_id, "by": by, "reason": reason},
    )


def cut_off(
    session: Session, res: IssuedResource, adapter: ProviderAdapter, *, reason: str,
    actor: CurrentUser | None = None,
) -> None:
    """Disable a key or stop a pod whose credential or grant was revoked. Something the
    provider no longer knows is recorded as unusable too (disabled key, terminated pod).
    Raises AdapterError if the provider is down; the next sync tries again."""
    details = {"group_id": res.group_id, "user_id": res.user_id, "by": "revocation", "reason": reason}
    if res.provider is Provider.openrouter:
        try:
            adapter.stop(res.external_ref)  # OpenRouter: disable the key
        except ResourceNotFound:
            details["already_gone"] = True
        res.status = ResourceStatus.disabled
        session.add(res)
        write_audit(session, actor, "key.disable", "issued_resource", res.id, details)
        return

    if res.status != ResourceStatus.running:
        return
    try:
        stop_pod(session, res, adapter, by="revocation", reason=reason, actor=actor)
    except ResourceNotFound:
        res.status = ResourceStatus.terminated
        _set_details(res, ready=None)
        session.add(res)
        write_audit(session, actor, "pod.gone", "issued_resource", res.id, {**details, "during": "revocation"})


def apply_key_limit(
    session: Session, res: IssuedResource, adapter: ProviderAdapter, usage: Decimal, available: Decimal,
    result: WatchdogResult,
) -> None:
    """limit = usage + what is left. Nothing left: the key is capped. Money back after a
    top-up: the key re-opens. Only calls OpenRouter when something changes."""
    limit = key_limit(usage, available)
    new_status = ResourceStatus.capped if available <= ZERO else ResourceStatus.active
    if limit == res.current_limit and new_status == res.status:
        return
    adapter.set_limit(res.external_ref, limit)

    if new_status is ResourceStatus.capped and res.status is not ResourceStatus.capped:
        action = "key.cap"
        result.keys_capped += 1
    elif new_status is ResourceStatus.active and res.status is ResourceStatus.capped:
        action = "key.reopen"
        result.keys_reopened += 1
    else:
        action = "key.set_limit"
        result.limits_set += 1
    write_audit(
        session, None, action, "issued_resource", res.id,
        {"group_id": res.group_id, "user_id": res.user_id, "by": "watchdog",
         "from": res.current_limit, "to": limit, "usage": usage, "available": available},
    )
    res.current_limit = limit
    res.status = new_status
    session.add(res)


# ====================================================================== notifications
def _alert(session: Session, group: Group, team: Totals, status: BudgetStatus, period: str,
           group_entries: list[LedgerEntry], result: WatchdogResult) -> None:
    """One notification per threshold per month, re-armed by any new money for the team."""
    if status is BudgetStatus.ok:
        return
    if team.funded <= ZERO and team.spent <= ZERO:
        return  # a team that never got a budget and spends nothing: nothing to say

    exhausted = status is BudgetStatus.exhausted
    action = EXHAUSTED_ACTION if exhausted else WARN_ACTION
    since = max(
        [period_start(period)]
        + [e.created_at for e in group_entries if e.scope_type == ScopeType.group and e.type in FUNDING]
    )
    already = session.exec(
        select(AuditLog.id).where(
            AuditLog.action == action,
            AuditLog.target_type == "group",
            AuditLog.target_id == str(group.id),
            AuditLog.at >= since,
        )
    ).first()
    if already is not None:
        return

    pct = int(team.used_fraction * 100)
    if exhausted:
        message = (
            f"{group.name} has used all of this month's {_usd(team.funded)} budget "
            f"({_usd(team.spent)} spent). Running pods were stopped and API keys capped "
            "until next month or a top-up from your professor."
        )
    else:
        message = (
            f"{group.name} has used {pct}% of this month's {_usd(team.funded)} budget "
            f"({_usd(team.spent)} spent, {_usd(team.remaining)} left)."
        )
    session.add(Notification(group_id=group.id, message=message, level="critical" if exhausted else "warning"))
    write_audit(
        session, None, action, "group", group.id,
        {"period": period, "funded": team.funded, "spent": team.spent, "percent": pct},
    )
    result.warnings += 1


# ====================================================================== the two passes the sync runs
def sweep_revoked(session: Session, lookup: Lookup, result: WatchdogResult) -> None:
    """Cut off whatever is still live under a revoked credential or grant (the revoke hook
    may have found the provider down)."""
    live = session.exec(select(IssuedResource).where(IssuedResource.status.in_(ACTIONABLE))).all()  # type: ignore[attr-defined]
    for res in live:
        reason = lookup.cut_off_reason(res)
        adapter = lookup.adapter(res.credential_id)
        if reason is None or adapter is None:
            continue
        if res.provider is Provider.runpod and res.status != ResourceStatus.running:
            continue
        try:
            cut_off(session, res, adapter, reason=reason)
            result.cut_off += 1
        except AdapterError as e:
            result.errors.append(f"cut off {res.id}: {type(e).__name__}")


def enforce_budgets(session: Session, period: str, lookup: Lookup, result: WatchdogResult) -> None:
    """Warn at 80%, stop and cap at 100%, and keep every live key's limit in step."""
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.period == period)).all()
    by_group: dict[uuid.UUID, list[LedgerEntry]] = defaultdict(list)
    for e in entries:
        if e.group_id is not None:
            by_group[e.group_id].append(e)

    with_user_budget = {
        (p.scope_id, p.group_id)
        for p in session.exec(select(BudgetPolicy).where(BudgetPolicy.scope_type == ScopeType.user)).all()
    }
    resources: dict[uuid.UUID, list[IssuedResource]] = defaultdict(list)
    for res in session.exec(select(IssuedResource).where(IssuedResource.status.in_(ACTIONABLE))).all():  # type: ignore[attr-defined]
        resources[res.group_id].append(res)

    for group in session.exec(select(Group)).all():
        rows = by_group[group.id]
        team = team_totals(rows, group.id, period)
        _alert(session, group, team, budget_status(team), period, rows, result)

        for res in resources[group.id]:
            if lookup.cut_off_reason(res) is not None:
                continue  # sweep_revoked's job
            adapter = lookup.adapter(res.credential_id)
            if adapter is None:
                continue
            user = user_totals(rows, res.user_id, group.id, period) if (res.user_id, group.id) in with_user_budget else None
            available = available_to_user(team, user)
            try:
                if res.provider is Provider.openrouter:
                    snap = last_snapshot(session, res.id)
                    apply_key_limit(session, res, adapter, snap.cumulative_usd if snap else ZERO, available, result)
                elif res.status == ResourceStatus.running and available <= ZERO:
                    whose = "team" if team.remaining <= ZERO else "user"
                    stop_pod(session, res, adapter, by="watchdog", reason=f"{whose} budget used up")
                    result.pods_stopped += 1
            except AdapterError as e:
                result.errors.append(f"enforce {res.id}: {type(e).__name__}")
                log.warning("watchdog: could not act on %s (%s)", res.id, type(e).__name__)


# ====================================================================== the revoke hook (Person 1's A3)
@on_credential_revoked
def cut_off_on_revoke(session: Session, actor: CurrentUser | None, event: CredentialRevoked) -> None:
    """Revoking a credential (or one professor's grant) cuts off its keys and pods at once,
    in the revoke request's own transaction. A provider that is down is left for the sync."""
    q = select(IssuedResource).where(
        IssuedResource.credential_id == event.credential_id,
        IssuedResource.status.in_(ACTIONABLE),  # type: ignore[attr-defined]
    )
    if event.professor_id is not None:
        q = q.join(Group, Group.id == IssuedResource.group_id).where(Group.professor_id == event.professor_id)
    lookup = Lookup(session)
    for res in session.exec(q).all():
        adapter = lookup.adapter(res.credential_id)
        if adapter is None:
            continue
        try:
            cut_off(session, res, adapter, reason=event.action, actor=actor)
        except AdapterError as e:
            log.warning("revoke: could not cut off %s now (%s); the next sync will", res.id, type(e).__name__)