"""Groups: read endpoints (A2). Create/edit/suspend come with A4/A5."""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import select

from app.core.scoping import ScopeDep
from app.models import Group, GroupMember, MemberStatus, User

router = APIRouter(prefix="/groups", tags=["groups"])


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
