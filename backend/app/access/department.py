"""Department endpoints (A10): the department admin adds and removes professors and sets,
per professor, a monthly budget ceiling or "tracking only" (D1).

    GET  /professors                        admin: his department's professors. A professor:
                                            only himself. A user: none.
    POST /professors                        admin: add (a new email gets a Supabase invite)
    DELETE /professors/{id}                 admin: remove (refused while he has active
                                            members, keys or pods)
    PUT  /professors/{id}/budget-setting    admin: ceiling or tracking only

Ceiling vs tracking (D1): saved as his professor-level budget policy. monthly_amount = the
ceiling, or NULL = tracking only (observe and bill, no ceiling). The change takes effect THIS
month: a professor with no allocation yet this month gets one for the new ceiling; a changed
ceiling writes the difference as an adjustment labelled CEILING_NOTE. The ledger is
append-only (D8), so nothing is ever edited. Top-ups are separate adjustments and untouched.

Removal is soft (professors.removed_at): his groups' ledger history must keep pointing at
real rows. A removed professor loses the professor role at login, his key selections are
cleared and his department grants are revoked (through the revoke hook, like any revoke).
Adding the same person again restores the role.

Only the department admin does any of this. Approving access, issuing keys and setting
group budgets stay with each professor (D21).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func
from sqlmodel import Session, select

from app.access.credentials import CredentialRevoked, _run_revoke_hooks
from app.budget.ledger import ZERO, period_of, professor_totals
from app.core.audit import write_audit
from app.core.invite import send_invite
from app.core.money import Money, NonNegativeMoney
from app.core.scoping import Scope, ScopeDep
from app.models import (
    BudgetPolicy,
    CredentialGrant,
    GrantStatus,
    Group,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    MemberStatus,
    Professor,
    ProfessorProviderBinding,
    ProviderCredential,
    ResourceStatus,
    ScopeType,
    User,
)
from app.models.base import utcnow

router = APIRouter(prefix="/professors", tags=["department"])

CEILING_NOTE = "Ceiling change"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LIVE = (ResourceStatus.active, ResourceStatus.capped, ResourceStatus.running, ResourceStatus.stopped)


# ====================================================================== API models
class BudgetSettingIO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ceiling", "tracking"]
    monthly_amount: NonNegativeMoney | None = None
    rollover: bool = True
    rollover_cap: NonNegativeMoney | None = None  # None = the default, 1 x monthly_amount (D2)

    @model_validator(mode="after")
    def _amount_matches_mode(self) -> "BudgetSettingIO":
        if self.mode == "ceiling" and self.monthly_amount is None:
            raise ValueError("monthly_amount is required for a ceiling")
        if self.mode == "tracking" and self.monthly_amount is not None:
            raise ValueError("monthly_amount must be null for tracking only")
        return self


class BudgetSettingOut(BaseModel):
    mode: Literal["ceiling", "tracking"]
    monthly_amount: Money | None
    rollover: bool
    rollover_cap: Money | None


class ProfessorOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    email: str
    department_id: uuid.UUID
    budget_setting: BudgetSettingOut
    group_count: int


class ProfessorAddedOut(ProfessorOut):
    invited: bool


class ProfessorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not a valid email address")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is empty")
        return v


# ====================================================================== helpers
def _policy(session: Session, professor_id: uuid.UUID) -> BudgetPolicy | None:
    return session.exec(
        select(BudgetPolicy).where(
            BudgetPolicy.scope_type == ScopeType.professor,
            BudgetPolicy.scope_id == professor_id,
            BudgetPolicy.group_id.is_(None),  # type: ignore[union-attr]
        )
    ).first()


def _setting_out(policy: BudgetPolicy | None) -> BudgetSettingOut:
    """No policy at all reads as tracking only."""
    if policy is None or policy.monthly_amount is None:
        return BudgetSettingOut(
            mode="tracking", monthly_amount=None,
            rollover=policy.rollover if policy else True, rollover_cap=None,
        )
    return BudgetSettingOut(
        mode="ceiling", monthly_amount=policy.monthly_amount,
        rollover=policy.rollover, rollover_cap=policy.rollover_cap,
    )


def _out(session: Session, prof: Professor) -> ProfessorOut:
    user = session.get(User, prof.user_id)
    assert user is not None
    groups = session.exec(select(func.count()).select_from(Group).where(Group.professor_id == prof.id)).one()
    return ProfessorOut(
        id=prof.id, user_id=user.id, name=user.name, email=user.email, department_id=prof.department_id,
        budget_setting=_setting_out(_policy(session, prof.id)), group_count=int(groups),
    )


def _require_admin_department(scope: Scope) -> uuid.UUID:
    if not scope.me.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the department admin manages professors")
    if len(scope.me.admin_department_ids) != 1:
        raise HTTPException(422, "You administer several departments")
    (dept,) = scope.me.admin_department_ids
    return dept


def _require_admin_of(scope: Scope, professor_id: uuid.UUID) -> Professor:
    """404 outside scope (or already removed); 403 unless the caller administers his department."""
    prof = scope.require_professor(professor_id)
    if prof.removed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professor not found")
    if not scope.me.is_admin or prof.department_id not in scope.me.admin_department_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the department admin manages professors")
    return prof


def _group_ids(session: Session, professor_id: uuid.UUID) -> list[uuid.UUID]:
    return list(session.exec(select(Group.id).where(Group.professor_id == professor_id)).all())


# ====================================================================== endpoints
@router.get("", response_model=list[ProfessorOut])
def list_professors(scope: ScopeDep) -> list[ProfessorOut]:
    """Admin: every current professor of his department. Professor: himself (to see his own
    ceiling). User: none (budget ceilings are not students' business)."""
    session, me = scope.session, scope.me
    q = select(Professor).where(Professor.removed_at.is_(None))  # type: ignore[union-attr]
    if me.is_admin:
        q = q.where(
            (Professor.department_id.in_(me.admin_department_ids)) | (Professor.id == me.professor_id)  # type: ignore[attr-defined]
        )
    elif me.is_professor:
        q = q.where(Professor.id == me.professor_id)
    else:
        return []
    profs = session.exec(q.join(User, User.id == Professor.user_id).order_by(User.name)).all()
    return [_out(session, p) for p in profs]


@router.post("", response_model=ProfessorAddedOut, status_code=status.HTTP_201_CREATED)
def add_professor(body: ProfessorIn, scope: ScopeDep) -> ProfessorAddedOut:
    """A known email gets the professor role (or gets it back after a removal); a new email
    creates the user and sends a Supabase invite. Starts as tracking only until the
    department sets a ceiling."""
    session = scope.session
    dept = _require_admin_department(scope)

    user = session.exec(select(User).where(func.lower(User.email) == body.email)).first()
    new_user = user is None
    if user is None:
        user = User(name=body.name, email=body.email)
        session.add(user)
        session.flush()

    prof = session.exec(select(Professor).where(Professor.user_id == user.id)).first()
    restored = False
    if prof is not None:
        if prof.removed_at is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Already a professor")
        if prof.department_id != dept:
            raise HTTPException(status.HTTP_409_CONFLICT, "This person was a professor in another department")
        prof.removed_at = None
        restored = True
    else:
        prof = Professor(department_id=dept, user_id=user.id)
    session.add(prof)
    session.flush()

    invited = send_invite(user.email) if new_user else False
    write_audit(
        session, scope.me, "professor.add", "professor", prof.id,
        {"user_id": user.id, "email": user.email, "new_user": new_user, "restored": restored, "invited": invited},
    )
    session.commit()
    return ProfessorAddedOut(**_out(session, prof).model_dump(), invited=invited)


@router.delete("/{professor_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_professor(professor_id: uuid.UUID, scope: ScopeDep) -> Response:
    session = scope.session
    prof = _require_admin_of(scope, professor_id)
    groups = _group_ids(session, prof.id)

    if groups:
        active_member = session.exec(
            select(GroupMember.user_id).where(
                GroupMember.group_id.in_(groups), GroupMember.status == MemberStatus.active  # type: ignore[attr-defined]
            )
        ).first()
        live = session.exec(
            select(IssuedResource.id).where(
                IssuedResource.group_id.in_(groups), IssuedResource.status.in_(_LIVE)  # type: ignore[attr-defined]
            )
        ).first()
        if active_member is not None or live is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This professor still has groups with active members, keys or pods. "
                "He must suspend or remove his members and stop his resources first.",
            )

    # He no longer uses any credential, and the department's grants to him end.
    for b in session.exec(select(ProfessorProviderBinding).where(ProfessorProviderBinding.professor_id == prof.id)).all():
        session.delete(b)
    grants = session.exec(
        select(CredentialGrant).where(CredentialGrant.professor_id == prof.id, CredentialGrant.status == GrantStatus.active)
    ).all()
    for g in grants:
        g.status = GrantStatus.revoked
        session.add(g)
        cred = session.get(ProviderCredential, g.credential_id)
        if cred is not None:
            _run_revoke_hooks(session, scope.me, CredentialRevoked(cred.id, cred.provider, prof.id, "grant.revoke"))

    prof.removed_at = utcnow()
    session.add(prof)
    write_audit(
        session, scope.me, "professor.remove", "professor", prof.id,
        {"user_id": prof.user_id, "groups": groups, "grants_revoked": [g.id for g in grants]},
    )
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{professor_id}/budget-setting", response_model=ProfessorOut)
def set_budget_setting(professor_id: uuid.UUID, body: BudgetSettingIO, scope: ScopeDep) -> ProfessorOut:
    session = scope.session
    prof = _require_admin_of(scope, professor_id)
    period = period_of(utcnow())

    entries = list(session.exec(select(LedgerEntry).where(LedgerEntry.period == period, (
        ((LedgerEntry.scope_type == ScopeType.professor) & (LedgerEntry.scope_id == prof.id))
        | LedgerEntry.group_id.in_(_group_ids(session, prof.id))  # type: ignore[union-attr]
    ))).all())
    totals = professor_totals(entries, prof.id, _group_ids(session, prof.id), period)
    mine = [e for e in entries if e.scope_type == ScopeType.professor and e.scope_id == prof.id]
    base = sum(
        (e.amount for e in mine
         if e.type == LedgerType.allocation
         or (e.type == LedgerType.adjustment and (e.note or "").startswith(CEILING_NOTE))),
        ZERO,
    )
    has_allocation = any(e.type == LedgerType.allocation for e in mine)

    # What this month's ledger entry will be, and the funding it leaves him with.
    new_entry: LedgerEntry | None = None
    new_funded = totals.funded
    if body.monthly_amount is not None:
        amount = Decimal(body.monthly_amount)
        if not has_allocation:
            new_entry = LedgerEntry(
                scope_type=ScopeType.professor, scope_id=prof.id, period=period, type=LedgerType.allocation,
                amount=amount, note="Monthly allocation (ceiling set by the department)", created_by=scope.me.id,
            )
            new_funded = totals.funded + amount
        elif amount != base:
            delta = amount - base
            new_entry = LedgerEntry(
                scope_type=ScopeType.professor, scope_id=prof.id, period=period, type=LedgerType.adjustment,
                amount=delta, note=f"{CEILING_NOTE}: ${base} -> ${amount}", created_by=scope.me.id,
            )
            new_funded = totals.funded + delta
        if totals.allocated > new_funded:
            raise HTTPException(
                422,
                f"He has already given his groups ${totals.allocated} this month; "
                f"the ceiling must leave at least that much (it would leave ${new_funded}).",
            )

    policy = _policy(session, prof.id)
    old = _setting_out(policy)
    if policy is None:
        policy = BudgetPolicy(scope_type=ScopeType.professor, scope_id=prof.id, group_id=None)
    policy.monthly_amount = body.monthly_amount
    policy.rollover = body.rollover
    policy.rollover_cap = body.rollover_cap
    policy.updated_at = utcnow()
    session.add(policy)
    if new_entry is not None:
        session.add(new_entry)
    session.flush()

    new = _setting_out(policy)
    if new != old or new_entry is not None:
        write_audit(
            session, scope.me, "budget.update", "professor", prof.id,
            {"old": old.model_dump(), "new": new.model_dump(),
             "ledger_entry": {"type": new_entry.type, "amount": new_entry.amount} if new_entry else None},
        )
    session.commit()
    return _out(session, prof)


# ====================================================================== Sync now (C6)
# The department admin's "Sync now" button (D4). pg_cron calls POST /jobs/sync with the job
# secret; a person calls this one with his login instead. Same run_sync, same database lock,
# so a click during a scheduled run is simply skipped.
sync_router = APIRouter(prefix="/department", tags=["department"])


class SyncNowOut(BaseModel):
    resources_polled: int
    spend_entries: int
    skipped: bool  # another sync was still running
    errors: int  # credentials the providers did not answer for


class SyncStatusOut(BaseModel):
    last_synced_at: datetime | None  # newest usage reading in the department; None = never synced


@sync_router.get("/sync", response_model=SyncStatusOut)
def sync_status(scope: ScopeDep) -> SyncStatusOut:
    """When usage was last read for the department's groups (the dashboard's "last synced")."""
    if not scope.me.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the department admin sees the sync status")
    from app.budget.api import _last_synced  # same rule as the professor's summary

    groups = list(scope.session.exec(
        select(Group.id).join(Professor, Professor.id == Group.professor_id)
        .where(Professor.department_id.in_(scope.me.admin_department_ids))  # type: ignore[attr-defined]
    ).all())
    return SyncStatusOut(last_synced_at=_last_synced(scope.session, groups))


@sync_router.post("/sync", response_model=SyncNowOut)
def sync_now(scope: ScopeDep) -> SyncNowOut:
    if not scope.me.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the department admin can run a sync by hand")
    from app.jobs.sync import run_sync  # here, not at the top: jobs imports access modules

    result = run_sync(scope.session)  # commits its own work
    out = SyncNowOut(resources_polled=result.resources_polled, spend_entries=result.spend_entries,
                     skipped=result.skipped, errors=len(result.errors))
    write_audit(scope.session, scope.me, "sync.manual", "department",
                sorted(scope.me.admin_department_ids)[0], out.model_dump())
    scope.session.commit()
    return out


__all__ = ["CEILING_NOTE", "router", "sync_router"]
