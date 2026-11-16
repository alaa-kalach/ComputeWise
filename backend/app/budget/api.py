"""Budget endpoints (A4, B10). Every number is computed from the ledger (D8); no balance
is ever stored.

    GET  /budget/summary                            professor: his month at a glance
    GET  /groups/{id}/budget                        anyone who sees the group (D9: own limit too)
    PUT  /groups/{id}/budget-policy                 its professor: monthly amount and rollover
    GET  /groups/{id}/member-budgets                its professor: each member's spend and limit
    PUT  /groups/{id}/members/{uid}/budget-policy   its professor: a member's personal limit
    POST /groups/{id}/top-ups                       its professor: extra money this month (B10)
    POST /groups/{id}/corrections                   its professor: fix a mistake, + or - (B10)

Changes apply to THIS month at once and are written as new ledger lines, never edits (D8):
the first budget of the month is an `allocation`; a later change is an `adjustment` for the
difference labelled BUDGET_NOTE (like CEILING_NOTE in department.py). Money given to groups
never exceeds what the professor has: his funding this month if he has a ceiling, and the
sum of his department grant ceilings if every credential he uses is a granted one.
Every change writes an audit entry before the commit (D12).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlmodel import Session, select

from app.budget.ledger import (
    ZERO,
    Totals,
    available_to_user,
    money,
    period_of,
    professor_totals,
    team_totals,
    user_totals,
)
from app.budget.policies import OverAllocation, check_allocation
from app.budget.topups import correction_entry, top_up_entry
from app.core.audit import write_audit
from app.core.money import Money, NonNegativeMoney, PositiveMoney
from app.core.scoping import ScopeDep
from app.models import (
    BudgetPolicy,
    CredentialGrant,
    GrantStatus,
    Group,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    OwnerType,
    ProfessorProviderBinding,
    Provider,
    ProviderCredential,
    ScopeType,
    UsageSnapshot,
)
from app.models.base import utcnow

router = APIRouter(tags=["budgets"])

BUDGET_NOTE = "Budget change"


# ====================================================================== API models
class TotalsOut(BaseModel):
    funded: Money
    spent: Money
    remaining: Money

    @classmethod
    def of(cls, t: Totals) -> "TotalsOut":
        return cls(funded=t.funded, spent=t.spent, remaining=t.remaining)


class PolicyOut(BaseModel):
    monthly_amount: Money
    rollover: bool
    rollover_cap: Money | None  # None = the default, 1 x monthly_amount (D2)


class ByProviderOut(BaseModel):
    openrouter: Money
    runpod: Money


class FundingOut(BaseModel):
    label: str
    billable: bool | None  # None = mixed: one provider paid by the department, the other not


class GroupBudgetOut(BaseModel):
    group_id: uuid.UUID
    period: str
    team: TotalsOut
    mine: TotalsOut | None
    available: Money
    policy: PolicyOut | None
    rolled_over: Money
    by_provider: ByProviderOut
    funding: FundingOut | None


class BudgetSummaryOut(BaseModel):
    period: str
    ceiling: Money | None
    allocated: Money
    unallocated: Money | None
    spent: Money
    last_synced_at: datetime | None


class MemberBudgetOut(BaseModel):
    user_id: uuid.UUID
    spent: Money
    limit: TotalsOut | None  # None = shares the team budget (D9)


class BudgetPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_amount: NonNegativeMoney
    rollover: bool = True
    rollover_cap: NonNegativeMoney | None = None


class MemberLimitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_amount: NonNegativeMoney | None  # None removes the limit


class _NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("write a reason")
        return v.strip()


class TopUpIn(_NoteIn):
    amount: PositiveMoney


class CorrectionIn(_NoteIn):
    amount: Money

    @field_validator("amount")
    @classmethod
    def _not_zero(cls, v: Decimal) -> Decimal:
        if v == ZERO:
            raise ValueError("a correction of 0 changes nothing")
        return v


# ====================================================================== helpers
def current_period() -> str:
    return period_of(utcnow())


def _sum(entries: Iterable[LedgerEntry]) -> Decimal:
    return money(sum((e.amount for e in entries), ZERO))


def _entries(
    session: Session, period: str, group_ids: list[uuid.UUID], professor_id: uuid.UUID | None = None
) -> list[LedgerEntry]:
    """This month's ledger lines of these groups, plus the professor's own lines if given."""
    cond = LedgerEntry.group_id.in_(group_ids)  # type: ignore[union-attr]
    if professor_id is not None:
        cond = cond | ((LedgerEntry.scope_type == ScopeType.professor) & (LedgerEntry.scope_id == professor_id))
    return list(session.exec(select(LedgerEntry).where(LedgerEntry.period == period, cond)).all())


def _policy(
    session: Session, scope_type: ScopeType, scope_id: uuid.UUID, group_id: uuid.UUID | None = None
) -> BudgetPolicy | None:
    return session.exec(
        select(BudgetPolicy).where(
            BudgetPolicy.scope_type == scope_type,
            BudgetPolicy.scope_id == scope_id,
            BudgetPolicy.group_id == group_id,
        )
    ).first()


def _funding(session: Session, professor_id: uuid.UUID) -> FundingOut | None:
    """Which credentials pay for this professor's groups (his bindings, one per provider)."""
    creds = session.exec(
        select(ProviderCredential)
        .join(ProfessorProviderBinding, ProfessorProviderBinding.credential_id == ProviderCredential.id)
        .where(ProfessorProviderBinding.professor_id == professor_id)
        .order_by(ProviderCredential.provider)
    ).all()
    if not creds:
        return None
    kinds = {c.owner_type == OwnerType.department for c in creds}
    return FundingOut(label=" + ".join(c.label for c in creds), billable=kinds.pop() if len(kinds) == 1 else None)


def _last_synced(session: Session, group_ids: list[uuid.UUID]) -> datetime | None:
    if not group_ids:
        return None
    return session.exec(
        select(func.max(UsageSnapshot.polled_at))
        .join(IssuedResource, IssuedResource.id == UsageSnapshot.issued_resource_id)
        .where(IssuedResource.group_id.in_(group_ids))  # type: ignore[attr-defined]
    ).one()


def build_group_budget(session: Session, group: Group, viewer_id: uuid.UUID) -> GroupBudgetOut:
    """A group's budget as `viewer_id` sees it. The change endpoints answer with this too."""
    period = current_period()
    entries = _entries(session, period, [group.id])
    team = team_totals(entries, group.id, period)
    mine = None
    if _policy(session, ScopeType.user, viewer_id, group.id) is not None:
        mine = user_totals(entries, viewer_id, group.id, period)
    policy = _policy(session, ScopeType.group, group.id, group.id)
    spend = [e for e in entries if e.type == LedgerType.spend]
    return GroupBudgetOut(
        group_id=group.id,
        period=period,
        team=TotalsOut.of(team),
        mine=TotalsOut.of(mine) if mine else None,
        available=available_to_user(team, mine),
        policy=(
            PolicyOut(monthly_amount=policy.monthly_amount, rollover=policy.rollover, rollover_cap=policy.rollover_cap)
            if policy is not None and policy.monthly_amount is not None
            else None
        ),
        rolled_over=_sum(e for e in entries if e.scope_type == ScopeType.group and e.type == LedgerType.rollover),
        by_provider=ByProviderOut(
            openrouter=-_sum(e for e in spend if e.provider == Provider.openrouter),
            runpod=-_sum(e for e in spend if e.provider == Provider.runpod),
        ),
        funding=_funding(session, group.professor_id),
    )


def _group_ids(session: Session, professor_id: uuid.UUID) -> list[uuid.UUID]:
    return list(session.exec(select(Group.id).where(Group.professor_id == professor_id)).all())


def _grant_limit(session: Session, professor_id: uuid.UUID) -> Decimal | None:
    """If every credential he uses is a department grant with a ceiling, he can't give his
    groups more than those ceilings together. His own credential = no grant limit."""
    creds = session.exec(
        select(ProviderCredential)
        .join(ProfessorProviderBinding, ProfessorProviderBinding.credential_id == ProviderCredential.id)
        .where(ProfessorProviderBinding.professor_id == professor_id)
    ).all()
    if not creds or any(c.owner_type != OwnerType.department for c in creds):
        return None
    grants = session.exec(
        select(CredentialGrant).where(
            CredentialGrant.professor_id == professor_id,
            CredentialGrant.status == GrantStatus.active,
            CredentialGrant.credential_id.in_([c.id for c in creds]),  # type: ignore[attr-defined]
        )
    ).all()
    if len(grants) < len(creds) or any(g.ceiling_usd is None for g in grants):
        return None
    return money(sum((g.ceiling_usd for g in grants if g.ceiling_usd is not None), ZERO))


def _check_can_give(session: Session, professor_id: uuid.UUID, extra: Decimal) -> None:
    """Raise OverAllocation if giving `extra` more to his groups this month passes his limit."""
    if extra <= ZERO:
        return  # taking money back is always allowed
    period, group_ids = current_period(), _group_ids(session, professor_id)
    totals = professor_totals(_entries(session, period, group_ids, professor_id), professor_id, group_ids, period)
    policy = _policy(session, ScopeType.professor, professor_id)
    limits = [totals.funded] if policy is not None and policy.monthly_amount is not None else []
    grant = _grant_limit(session, professor_id)
    if grant is not None:
        limits.append(grant)
    if limits:
        check_allocation(min(limits), totals.allocated, extra)


def _over(e: OverAllocation) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": f"That's more than you have left to give: ${e.available:,.2f}.",
            "unallocated": str(money(e.available)),
        },
    )


def _base(entries: list[LedgerEntry], scope_type: ScopeType, scope_id: uuid.UUID) -> tuple[Decimal, bool]:
    """This month's budget so far (allocation + earlier budget changes, not top-ups) and
    whether an allocation line exists yet."""
    mine = [e for e in entries if e.scope_type == scope_type and e.scope_id == scope_id]
    base = _sum(
        e for e in mine
        if e.type == LedgerType.allocation
        or (e.type == LedgerType.adjustment and (e.note or "").startswith(BUDGET_NOTE))
    )
    return base, any(e.type == LedgerType.allocation for e in mine)


def _budget_line(
    entries: list[LedgerEntry], scope_type: ScopeType, scope_id: uuid.UUID, group_id: uuid.UUID,
    amount: Decimal, created_by: uuid.UUID,
) -> LedgerEntry | None:
    """The ledger line that makes this month's budget equal `amount`, or None if it already is."""
    base, has_allocation = _base(entries, scope_type, scope_id)
    common = dict(scope_type=scope_type, scope_id=scope_id, group_id=group_id, period=current_period(),
                  created_by=created_by)
    if not has_allocation:
        if amount == ZERO:
            return None
        return LedgerEntry(**common, type=LedgerType.allocation, amount=amount, note="Monthly allocation")
    if amount == base:
        return None
    return LedgerEntry(**common, type=LedgerType.adjustment, amount=amount - base,
                       note=f"{BUDGET_NOTE}: ${base} -> ${amount}")


def _policy_out(p: BudgetPolicy | None) -> dict | None:
    if p is None:
        return None
    return {"monthly_amount": p.monthly_amount, "rollover": p.rollover, "rollover_cap": p.rollover_cap}


def _member_budget(session: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> MemberBudgetOut:
    period = current_period()
    entries = _entries(session, period, [group_id])
    spent = ZERO - _sum(e for e in entries if e.type == LedgerType.spend and e.scope_id == user_id)
    has_limit = _policy(session, ScopeType.user, user_id, group_id) is not None
    return MemberBudgetOut(
        user_id=user_id, spent=spent,
        limit=TotalsOut.of(user_totals(entries, user_id, group_id, period)) if has_limit else None,
    )


# ====================================================================== endpoints
@router.get("/budget/summary", response_model=BudgetSummaryOut)
def budget_summary(scope: ScopeDep) -> BudgetSummaryOut:
    if not scope.me.is_professor or scope.me.professor_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only professors have a budget summary")
    session, period, prof_id = scope.session, current_period(), scope.me.professor_id
    group_ids = list(session.exec(select(Group.id).where(Group.professor_id == prof_id)).all())
    totals = professor_totals(_entries(session, period, group_ids, prof_id), prof_id, group_ids, period)
    policy = _policy(session, ScopeType.professor, prof_id)
    has_ceiling = policy is not None and policy.monthly_amount is not None  # else tracking only (D1)
    return BudgetSummaryOut(
        period=period,
        ceiling=totals.funded if has_ceiling else None,
        allocated=totals.allocated,
        unallocated=totals.unallocated if has_ceiling else None,
        spent=totals.spent,
        last_synced_at=_last_synced(session, group_ids),
    )


@router.get("/groups/{group_id}/budget", response_model=GroupBudgetOut)
def group_budget(group_id: uuid.UUID, scope: ScopeDep) -> GroupBudgetOut:
    group = scope.require_group(group_id)  # 404 if outside the caller's scope (D11)
    return build_group_budget(scope.session, group, scope.me.id)


@router.put("/groups/{group_id}/budget-policy", response_model=GroupBudgetOut)
def set_group_budget(group_id: uuid.UUID, body: BudgetPolicyIn, scope: ScopeDep):
    session = scope.session
    group = scope.require_manage_group(group_id)  # 404 outside scope, 403 if not his group
    entries = _entries(session, current_period(), [group.id])
    line = _budget_line(entries, ScopeType.group, group.id, group.id, body.monthly_amount, scope.me.id)
    try:
        _check_can_give(session, group.professor_id, line.amount if line else ZERO)
    except OverAllocation as e:
        return _over(e)

    policy = _policy(session, ScopeType.group, group.id, group.id)
    old = _policy_out(policy)
    if policy is None:
        policy = BudgetPolicy(scope_type=ScopeType.group, scope_id=group.id, group_id=group.id)
    policy.monthly_amount = body.monthly_amount
    policy.rollover = body.rollover
    policy.rollover_cap = body.rollover_cap
    policy.updated_at = utcnow()
    session.add(policy)
    if line is not None:
        session.add(line)
    session.flush()
    write_audit(session, scope.me, "budget.update", "group", group.id, {
        "old": old, "new": _policy_out(policy),
        "ledger_entry": {"type": line.type, "amount": line.amount} if line else None,
    })
    session.commit()
    return build_group_budget(session, group, scope.me.id)


@router.get("/groups/{group_id}/member-budgets", response_model=list[MemberBudgetOut])
def member_budgets(group_id: uuid.UUID, scope: ScopeDep) -> list[MemberBudgetOut]:
    group = scope.require_manage_group(group_id)
    user_ids = scope.session.exec(select(GroupMember.user_id).where(GroupMember.group_id == group.id)).all()
    return [_member_budget(scope.session, group.id, uid) for uid in user_ids]


@router.put("/groups/{group_id}/members/{user_id}/budget-policy", response_model=MemberBudgetOut)
def set_member_limit(group_id: uuid.UUID, user_id: uuid.UUID, body: MemberLimitIn, scope: ScopeDep) -> MemberBudgetOut:
    session = scope.session
    group = scope.require_manage_group(group_id)
    if session.get(GroupMember, (group.id, user_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    policy = _policy(session, ScopeType.user, user_id, group.id)
    old = None if policy is None else str(policy.monthly_amount)

    if body.monthly_amount is None:  # back to sharing the team budget
        if policy is not None:
            session.delete(policy)
            write_audit(session, scope.me, "budget.update", "member", user_id,
                        {"group_id": group.id, "old": old, "new": None})
            session.commit()
        return _member_budget(session, group.id, user_id)

    entries = _entries(session, current_period(), [group.id])
    team = team_totals(entries, group.id, current_period())
    if body.monthly_amount > team.funded:  # a limit inside the team budget, not extra money (D9)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A student's limit can't be more than the team's budget: ${team.funded:,.2f}.",
        )
    line = _budget_line(entries, ScopeType.user, user_id, group.id, body.monthly_amount, scope.me.id)
    if policy is None:
        policy = BudgetPolicy(scope_type=ScopeType.user, scope_id=user_id, group_id=group.id, rollover=False)
    policy.monthly_amount = body.monthly_amount
    policy.updated_at = utcnow()
    session.add(policy)
    if line is not None:
        session.add(line)
    session.flush()
    write_audit(session, scope.me, "budget.update", "member", user_id, {
        "group_id": group.id, "old": old, "new": str(body.monthly_amount),
        "ledger_entry": {"type": line.type, "amount": line.amount} if line else None,
    })
    session.commit()
    return _member_budget(session, group.id, user_id)


@router.post("/groups/{group_id}/top-ups", response_model=GroupBudgetOut, status_code=status.HTTP_201_CREATED)
def top_up(group_id: uuid.UUID, body: TopUpIn, scope: ScopeDep):
    session = scope.session
    group = scope.require_manage_group(group_id)
    try:
        _check_can_give(session, group.professor_id, body.amount)
    except OverAllocation as e:
        return _over(e)
    entry = top_up_entry(ScopeType.group, group.id, current_period(), body.amount,
                         group_id=group.id, created_by=scope.me.id, note=body.note)
    session.add(entry)
    session.flush()
    write_audit(session, scope.me, "ledger.adjustment", "group", group.id,
                {"kind": "top_up", "amount": entry.amount, "note": entry.note, "entry_id": entry.id})
    session.commit()
    return build_group_budget(session, group, scope.me.id)


@router.post("/groups/{group_id}/corrections", response_model=GroupBudgetOut, status_code=status.HTTP_201_CREATED)
def correct(group_id: uuid.UUID, body: CorrectionIn, scope: ScopeDep):
    session = scope.session
    group = scope.require_manage_group(group_id)
    try:
        _check_can_give(session, group.professor_id, body.amount)  # only checks when adding money
    except OverAllocation as e:
        return _over(e)
    entry = correction_entry(ScopeType.group, group.id, current_period(), body.amount,
                             group_id=group.id, created_by=scope.me.id, note=body.note)
    session.add(entry)
    session.flush()
    write_audit(session, scope.me, "ledger.adjustment", "group", group.id,
                {"kind": "correction", "amount": entry.amount, "note": entry.note, "entry_id": entry.id})
    session.commit()
    return build_group_budget(session, group, scope.me.id)