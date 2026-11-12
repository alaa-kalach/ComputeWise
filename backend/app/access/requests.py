"""Access requests (A5): a student asks for OpenRouter or RunPod access in one of his
groups; only that group's professor approves or rejects (D21). The department sees the
requests but cannot decide them. Every step is audited.

What approval means:
    RunPod      the student can launch his own pods in this group (B4 checks it).
    OpenRouter  the student can get his key once with POST /keys (Person 3's B3 checks it).

For B3 and B4:

    require_approved_access(session, me.id, group_id, Provider.runpod)   # 403 if not
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.access.credentials import resolve_credential
from app.core.audit import write_audit
from app.core.scoping import ScopeDep
from app.models import AccessRequest, Group, GroupMember, MemberStatus, Provider, RequestStatus, User
from app.models.base import utcnow

router = APIRouter(prefix="/access-requests", tags=["access"])


# ====================================================================== API models
class AccessRequestOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    group_id: uuid.UUID
    group_name: str
    provider: Provider
    justification: str | None
    status: RequestStatus
    reason: str | None
    decided_by_name: str | None
    decided_at: datetime | None
    created_at: datetime


class AccessRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: uuid.UUID
    provider: Provider
    justification: str | None = Field(default=None, max_length=2000)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=2000)


# ====================================================================== for B3 and B4
def require_approved_access(session: Session, user_id: uuid.UUID, group_id: uuid.UUID, provider: Provider) -> None:
    """403 unless the user is an ACTIVE member of the group with an approved request for
    this provider. Call it before a student launches a pod (B4) or gets a key (B3)."""
    member = session.exec(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
    ).first()
    if member is None or member.status != MemberStatus.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not an active member of this group")
    approved = session.exec(
        select(AccessRequest.id).where(
            AccessRequest.user_id == user_id,
            AccessRequest.group_id == group_id,
            AccessRequest.provider == provider,
            AccessRequest.status == RequestStatus.approved,
        )
    ).first()
    if approved is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"You don't have approved {provider.value} access in this group. Request it first.",
        )


# ====================================================================== internals
_Decider = aliased(User)


def _query():
    return (
        select(AccessRequest, User.name, Group.name, _Decider.name)
        .join(User, User.id == AccessRequest.user_id)
        .join(Group, Group.id == AccessRequest.group_id)
        .outerjoin(_Decider, _Decider.id == AccessRequest.decided_by)
    )


def _out(row) -> AccessRequestOut:
    req, user_name, group_name, decided_by_name = row
    return AccessRequestOut(
        id=req.id,
        user_id=req.user_id,
        user_name=user_name,
        group_id=req.group_id,
        group_name=group_name,
        provider=req.provider,
        justification=req.justification,
        status=req.status,
        reason=req.reason,
        decided_by_name=decided_by_name,
        decided_at=req.decided_at,
        created_at=req.created_at,
    )


def _one(session: Session, request_id: uuid.UUID) -> AccessRequestOut:
    return _out(session.exec(_query().where(AccessRequest.id == request_id)).one())


def _decide_checks(scope, request_id: uuid.UUID) -> tuple[AccessRequest, Group]:
    req = scope.require_access_request(request_id)  # 404 outside scope
    group = scope.require_manage_group(req.group_id)  # 403 unless his professor
    if req.status != RequestStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This request is already {req.status.value}")
    return req, group


# ====================================================================== endpoints
@router.get("", response_model=list[AccessRequestOut])
def list_requests(
    scope: ScopeDep,
    status_: Annotated[RequestStatus | None, Query(alias="status")] = None,
    group_id: uuid.UUID | None = None,
) -> list[AccessRequestOut]:
    """User: his own. Professor: his groups'. Admin: the department's (read only). Newest first."""
    visible = select(AccessRequest.id).where(scope.access_requests().whereclause)  # type: ignore[arg-type]
    q = _query().where(AccessRequest.id.in_(visible))  # type: ignore[attr-defined]
    if status_ is not None:
        q = q.where(AccessRequest.status == status_)
    if group_id is not None:
        q = q.where(AccessRequest.group_id == group_id)
    q = q.order_by(AccessRequest.created_at.desc())  # type: ignore[attr-defined]
    return [_out(r) for r in scope.session.exec(q).all()]


@router.post("", response_model=AccessRequestOut, status_code=status.HTTP_201_CREATED)
def create_request(body: AccessRequestIn, scope: ScopeDep) -> AccessRequestOut:
    """A member asks for access in one of HIS groups."""
    session, me = scope.session, scope.me
    member = session.exec(
        select(GroupMember).where(GroupMember.group_id == body.group_id, GroupMember.user_id == me.id)
    ).first()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    if member.status != MemberStatus.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "You are suspended in this group")
    existing = session.exec(
        select(AccessRequest.status).where(
            AccessRequest.user_id == me.id,
            AccessRequest.group_id == body.group_id,
            AccessRequest.provider == body.provider,
            AccessRequest.status.in_([RequestStatus.pending, RequestStatus.approved]),  # type: ignore[attr-defined]
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have access" if existing == RequestStatus.approved else "You already have a pending request",
        )

    justification = (body.justification or "").strip() or None
    req = AccessRequest(user_id=me.id, group_id=body.group_id, provider=body.provider, justification=justification)
    session.add(req)
    session.flush()
    write_audit(
        session, me, "access_request.create", "access_request", req.id,
        {"group_id": req.group_id, "provider": req.provider},
    )
    session.commit()
    return _one(session, req.id)


@router.post("/{request_id}/approve", response_model=AccessRequestOut)
def approve_request(request_id: uuid.UUID, scope: ScopeDep) -> AccessRequestOut:
    """The group's professor only. Refused (409) if the student is no longer an active
    member, or if the professor has no usable credential for this provider."""
    session = scope.session
    req, group = _decide_checks(scope, request_id)
    member = session.exec(
        select(GroupMember).where(GroupMember.group_id == req.group_id, GroupMember.user_id == req.user_id)
    ).first()
    if member is None or member.status != MemberStatus.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "The student is no longer an active member of this group")
    cred = resolve_credential(session, group.professor_id, req.provider)  # 409 if none

    req.status = RequestStatus.approved
    req.decided_by = scope.me.id
    req.decided_at = utcnow()
    session.add(req)
    write_audit(
        session, scope.me, "access_request.approve", "access_request", req.id,
        {"user_id": req.user_id, "group_id": req.group_id, "provider": req.provider,
         "credential_id": cred.credential_id},
    )
    session.commit()
    return _one(session, req.id)


@router.post("/{request_id}/reject", response_model=AccessRequestOut)
def reject_request(request_id: uuid.UUID, scope: ScopeDep, body: RejectIn | None = None) -> AccessRequestOut:
    """The group's professor only. The reason is optional and shown to the student."""
    session = scope.session
    req, _ = _decide_checks(scope, request_id)
    reason = ((body.reason if body else None) or "").strip() or None

    req.status = RequestStatus.rejected
    req.decided_by = scope.me.id
    req.decided_at = utcnow()
    req.reason = reason
    session.add(req)
    write_audit(
        session, scope.me, "access_request.reject", "access_request", req.id,
        {"user_id": req.user_id, "group_id": req.group_id, "provider": req.provider, "reason": reason},
    )
    session.commit()
    return _one(session, req.id)
