"""FastAPI app. Each area adds its router below."""

import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlmodel import select

from app.access import credentials, groups, requests
from app.core.auth import Role
from app.core.config import get_settings
from app.core.scoping import ScopeDep
from app.models import Group, GroupMember, MemberStatus

app = FastAPI(title="ComputeWise API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


class MembershipOut(BaseModel):
    group_id: uuid.UUID
    group_name: str
    professor_id: uuid.UUID
    status: MemberStatus


class MeOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: Role  # primary role: picks the default dashboard
    roles: list[Role]  # every role held; an admin who is also a professor gets both views
    department_ids: list[uuid.UUID]
    professor_id: uuid.UUID | None
    memberships: list[MembershipOut]


@app.get("/me", response_model=MeOut, tags=["auth"])
def me(scope: ScopeDep) -> MeOut:
    """Who am I? The frontend calls this after login to pick the layout for the role."""
    rows = scope.session.exec(
        select(GroupMember, Group)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.user_id == scope.me.id)
        .order_by(Group.name)
    ).all()
    u = scope.me.user
    return MeOut(
        id=u.id,
        name=u.name,
        email=u.email,
        role=scope.me.role,
        roles=sorted(scope.me.roles, key=lambda r: list(Role).index(r)),
        department_ids=sorted(scope.me.department_ids),
        professor_id=scope.me.professor_id,
        memberships=[
            MembershipOut(group_id=g.id, group_name=g.name, professor_id=g.professor_id, status=m.status)
            for m, g in rows
        ],
    )


app.include_router(groups.router)
app.include_router(credentials.router)
app.include_router(requests.router)
