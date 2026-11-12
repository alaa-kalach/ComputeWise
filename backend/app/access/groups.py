"""Groups and members (A2 reads, A5 changes).

Only the group's own professor creates, renames and changes a group, adds, suspends,
reactivates or removes members (scope.require_manage_group, D21). The department sees every
group but manages none. Every change is audited.

Suspending a member stops his access in THIS group only (he may also be in another
professor's group): his OpenRouter keys there are disabled and his pods there are stopped,
never terminated (D3). Reactivating re-enables the keys the suspension disabled; pods stay
stopped and he starts them again himself.
"""

import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.access.credentials import adapter_for_credential
from app.adapters import AdapterError, ResourceNotFound
from app.core.audit import write_audit
from app.core.auth import CurrentUser
from app.core.invite import send_invite
from app.core.scoping import ScopeDep
from app.models import (
    Group,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    MemberStatus,
    Provider,
    ResourceStatus,
    ScopeType,
    User,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/groups", tags=["groups"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Keys and pods that can still spend (a stopped pod still costs storage, D3).
LIVE = (ResourceStatus.active, ResourceStatus.capped, ResourceStatus.running, ResourceStatus.stopped)


# ====================================================================== API models
class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    professor_id: uuid.UUID
    created_at: datetime


class MemberOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    status: MemberStatus


class GroupDetailOut(GroupOut):
    members: list[MemberOut]


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is empty")
        return v


class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    name: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not a valid email address")
        return v


class MemberAddedOut(MemberOut):
    invited: bool  # True if an invite email was sent


class MemberPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: MemberStatus


# ====================================================================== reads (A2)
@router.get("", response_model=list[GroupOut])
def list_groups(scope: ScopeDep) -> list[Group]:
    """Groups visible to the caller: all in the department (admin), his own (professor),
    or the ones he belongs to (user)."""
    return list(scope.session.exec(scope.groups()).all())


@router.get("/{group_id}", response_model=GroupDetailOut)
def get_group(group_id: uuid.UUID, scope: ScopeDep) -> GroupDetailOut:
    """404 if the group is outside the caller's scope (e.g. another professor's group)."""
    group = scope.require_group(group_id)
    rows = scope.session.exec(
        select(GroupMember, User)
        .join(User, User.id == GroupMember.user_id)
        .where(GroupMember.group_id == group.id, GroupMember.user_id.in_(scope.user_ids()))
        .order_by(User.name)
    ).all()
    return GroupDetailOut(
        **GroupOut.model_validate(group, from_attributes=True).model_dump(),
        members=[MemberOut(user_id=u.id, name=u.name, email=u.email, status=m.status) for m, u in rows],
    )


# ====================================================================== groups (A5)
def _name_taken(session: Session, professor_id: uuid.UUID, name: str, except_id: uuid.UUID | None = None) -> bool:
    q = select(Group.id).where(Group.professor_id == professor_id, func.lower(Group.name) == name.lower())
    if except_id is not None:
        q = q.where(Group.id != except_id)
    return session.exec(q).first() is not None


def _duplicate() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "You already have a group with that name")


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
def create_group(body: GroupIn, scope: ScopeDep) -> Group:
    me, session = scope.me, scope.session
    if not me.is_professor or me.professor_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only professors create groups")
    if _name_taken(session, me.professor_id, body.name):
        raise _duplicate()
    group = Group(professor_id=me.professor_id, name=body.name)
    session.add(group)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise _duplicate() from None
    write_audit(session, me, "group.create", "group", group.id, {"name": group.name})
    session.commit()
    session.refresh(group)
    return group


@router.patch("/{group_id}", response_model=GroupOut)
def rename_group(group_id: uuid.UUID, body: GroupIn, scope: ScopeDep) -> Group:
    session = scope.session
    group = scope.require_manage_group(group_id)
    if group.name == body.name:
        return group
    if _name_taken(session, group.professor_id, body.name, except_id=group.id):
        raise _duplicate()
    old = group.name
    group.name = body.name
    session.add(group)
    write_audit(session, scope.me, "group.rename", "group", group.id, {"old_name": old, "name": body.name})
    session.commit()
    session.refresh(group)
    return group


# ====================================================================== members (A5)
def _require_member(session: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> GroupMember:
    member = session.exec(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
    ).first()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    return member


def _member_out(session: Session, member: GroupMember) -> MemberOut:
    user = session.get(User, member.user_id)
    assert user is not None
    return MemberOut(user_id=user.id, name=user.name, email=user.email, status=member.status)


@router.post("/{group_id}/members", response_model=MemberAddedOut, status_code=status.HTTP_201_CREATED)
def add_member(group_id: uuid.UUID, body: MemberIn, scope: ScopeDep) -> MemberAddedOut:
    """Add by email. A known user is just added (the response never says which other groups
    he is in); a new email creates the user and sends a Supabase invite."""
    session = scope.session
    group = scope.require_manage_group(group_id)

    user = session.exec(select(User).where(func.lower(User.email) == body.email)).first()
    new_user = user is None
    if user is None:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(422, "This email is new: give the student's name too")
        user = User(name=name, email=body.email)
        session.add(user)
        session.flush()
    elif session.exec(
        select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id == user.id)
    ).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Already a member of this group")

    member = GroupMember(group_id=group.id, user_id=user.id)
    session.add(member)
    session.flush()
    invited = send_invite(user.email) if new_user else False
    write_audit(
        session, scope.me, "member.add", "group", group.id,
        {"user_id": user.id, "email": user.email, "new_user": new_user, "invited": invited},
    )
    session.commit()
    return MemberAddedOut(**_member_out(session, member).model_dump(), invited=invited)


def _set_resource_state(
    session: Session, actor: CurrentUser, res: IssuedResource, *, suspend: bool
) -> bool:
    """Disable/stop (suspend) or re-enable (reactivate) one resource at the provider.
    Returns False if the provider call failed; the resource is then left as it was."""
    is_key = res.provider == Provider.openrouter
    adapter = adapter_for_credential(session, res.credential_id)
    try:
        if adapter is not None:
            (adapter.stop if suspend else adapter.resume)(res.external_ref)
    except ResourceNotFound:
        # Gone at the provider: a deleted key or a terminated pod. Record that and move on.
        res.status = ResourceStatus.disabled if is_key else ResourceStatus.terminated
        res.details = {k: v for k, v in res.details.items() if k != "suspended"}
        session.add(res)
        return True
    except AdapterError as exc:
        log.warning("Could not %s resource %s: %s", "stop" if suspend else "resume", res.id, type(exc).__name__)
        return False

    if suspend:
        res.status = ResourceStatus.disabled if is_key else ResourceStatus.stopped
        res.details = {**res.details, "suspended": True}
        action = "key.disable" if is_key else "pod.stop"
    else:
        res.status = ResourceStatus.active
        res.details = {k: v for k, v in res.details.items() if k != "suspended"}
        action = "key.enable"
    session.add(res)
    write_audit(
        session, actor, action, "issued_resource", res.id,
        {"reason": "member.suspend" if suspend else "member.reactivate", "group_id": res.group_id,
         "user_id": res.user_id, "provider_call": adapter is not None},
    )
    return True


@router.patch("/{group_id}/members/{user_id}", response_model=MemberOut)
def set_member_status(group_id: uuid.UUID, user_id: uuid.UUID, body: MemberPatch, scope: ScopeDep) -> MemberOut:
    """Suspend or reactivate a member in THIS group only."""
    session = scope.session
    group = scope.require_manage_group(group_id)
    member = _require_member(session, group.id, user_id)
    if member.status == body.status:
        return _member_out(session, member)

    suspend = body.status == MemberStatus.suspended
    if suspend:
        targets = session.exec(
            select(IssuedResource).where(
                IssuedResource.group_id == group.id,
                IssuedResource.user_id == user_id,
                IssuedResource.status.in_([ResourceStatus.active, ResourceStatus.capped, ResourceStatus.running]),  # type: ignore[attr-defined]
            )
        ).all()
    else:
        # Only keys the suspension disabled come back. Pods stay stopped; he starts them himself.
        targets = [
            r for r in session.exec(
                select(IssuedResource).where(
                    IssuedResource.group_id == group.id,
                    IssuedResource.user_id == user_id,
                    IssuedResource.status.in_([ResourceStatus.disabled, ResourceStatus.stopped]),  # type: ignore[attr-defined]
                )
            ).all()
            if r.details.get("suspended")
        ]
    failed: list[uuid.UUID] = []
    for res in targets:
        if res.provider == Provider.runpod and not suspend:
            res.details = {k: v for k, v in res.details.items() if k != "suspended"}
            session.add(res)
            continue
        if not _set_resource_state(session, scope.me, res, suspend=suspend):
            failed.append(res.id)

    member.status = body.status
    session.add(member)
    write_audit(
        session, scope.me, "member.suspend" if suspend else "member.reactivate", "group", group.id,
        {"user_id": user_id, "resources": [r.id for r in targets], "provider_failed": failed},
    )
    session.commit()
    return _member_out(session, member)


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(group_id: uuid.UUID, user_id: uuid.UUID, scope: ScopeDep) -> Response:
    """Only if he never spent anything in this group and has no live key or pod in it;
    otherwise suspend him, so the ledger and the billing history stay consistent."""
    session = scope.session
    group = scope.require_manage_group(group_id)
    member = _require_member(session, group.id, user_id)

    spent = session.exec(
        select(LedgerEntry.id).where(
            LedgerEntry.group_id == group.id,
            LedgerEntry.scope_type == ScopeType.user,
            LedgerEntry.scope_id == user_id,
            LedgerEntry.type == LedgerType.spend,
        )
    ).first()
    live = session.exec(
        select(IssuedResource.id).where(
            IssuedResource.group_id == group.id,
            IssuedResource.user_id == user_id,
            IssuedResource.status.in_(LIVE),  # type: ignore[attr-defined]
        )
    ).first()
    if spent is not None or live is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This member has spend or a live key or pod in this group. Suspend him instead.",
        )
    session.delete(member)
    write_audit(session, scope.me, "member.remove", "group", group.id, {"user_id": user_id})
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
