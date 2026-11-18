"""B3: a student gets his OpenRouter key, once.

POST /keys (proposed in the contract, owner Person 3):
  - only after the professor approved his OpenRouter request (403 otherwise)
  - the key is created on OpenRouter with a credit limit = what he may still spend (D9:
    the team's remaining, or his own member limit if lower)
  - the plaintext key is returned in THIS response only. We store the key's hash, never the key.
  - asking again returns 409.

Added for the dashboards (C3, C4), as the mocks already behave (frontend/src/mocks/handlers.ts):
GET /keys                   keys in scope, never the secret: a student sees his own, a
                            professor every key in his groups, the department all (read only)
POST /keys/{id}/revoke      the group's professor only (D21). The key is disabled on
                            OpenRouter (its usage stays readable) and never turned back on.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlmodel import select

from app.access.credentials import adapter_for_credential, resolve_credential
from app.access.pods import available_budget
from app.access.requests import require_approved_access
from app.adapters import AdapterError, IssueRequest, ProviderUnavailable, ResourceNotFound, get_adapter
from app.core.audit import write_audit
from app.core.money import Money, quantize
from app.core.scoping import ScopeDep
from app.models import Group, IssuedResource, Provider, ResourceStatus, UsageSnapshot, User

log = logging.getLogger(__name__)
router = APIRouter(tags=["access"])


class KeyRequest(BaseModel):
    group_id: uuid.UUID


class IssuedKeyOut(BaseModel):
    resource_id: uuid.UUID
    key: str  # plaintext, shown only in this response
    limit_usd: Money


def _provider_error(exc: AdapterError) -> HTTPException:
    if isinstance(exc, ProviderUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OpenRouter can't be reached right now. Try again shortly.")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"OpenRouter refused the request: {exc}")


@router.post("/keys", response_model=IssuedKeyOut, status_code=status.HTTP_201_CREATED)
def get_my_key(body: KeyRequest, scope: ScopeDep) -> IssuedKeyOut:
    session, me = scope.session, scope.me
    group = scope.require_group(body.group_id)  # 404 outside scope
    require_approved_access(session, me.id, group.id, Provider.openrouter)  # 403

    already = session.exec(
        select(IssuedResource.id).where(
            IssuedResource.user_id == me.id,
            IssuedResource.group_id == group.id,
            IssuedResource.provider == Provider.openrouter,
        )
    ).first()
    if already is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "You already got your key for this group. It is shown only once.")

    limit = quantize(available_budget(session, group.id, me.id))
    if limit <= Decimal(0):
        raise HTTPException(status.HTTP_409_CONFLICT, "No budget is left in this group, so a key can't be issued. Tell your professor.")

    cred = resolve_credential(session, group.professor_id, Provider.openrouter)  # 409 if none
    adapter = get_adapter(Provider.openrouter, cred.secret)
    name = f"cw-{str(group.id)[:8]}-{str(me.id)[:8]}"  # same naming as pods (D17)
    try:
        issued = adapter.issue(IssueRequest(name=name, user_id=str(me.id), group_id=str(group.id), limit_usd=limit))
    except AdapterError as exc:
        raise _provider_error(exc) from None
    if not issued.secret:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OpenRouter did not return a key")

    res = IssuedResource(
        user_id=me.id,
        group_id=group.id,
        provider=Provider.openrouter,
        credential_id=cred.credential_id,
        external_ref=issued.external_ref,  # the key HASH
        current_limit=limit,
        status=ResourceStatus.active,
        details={"name": name},
    )
    session.add(res)
    try:
        session.flush()
        write_audit(
            session, me, "key.issue", "issued_resource", res.id,
            {"group_id": group.id, "external_ref": res.external_ref, "limit": limit, "credential_id": cred.credential_id},
        )
        session.commit()
    except Exception:
        # Don't leave a live key nobody tracks.
        session.rollback()
        try:
            adapter.revoke(issued.external_ref)
        except AdapterError:
            log.error("Key %s issued but not recorded, and deleting it failed", issued.external_ref)
        raise
    return IssuedKeyOut(resource_id=res.id, key=issued.secret, limit_usd=limit)


# ====================================================================== C3 / C4: list and revoke
class KeySummaryOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    group_name: str
    status: Literal["active", "capped", "disabled"]
    limit_usd: Money | None
    usage_usd: Money  # total since the key was issued, as of the last sync
    created_at: datetime
    user_id: uuid.UUID
    user_name: str


def _usage(session, resource_id: uuid.UUID) -> Decimal:
    total = session.exec(
        select(UsageSnapshot.cumulative_usd)
        .where(UsageSnapshot.issued_resource_id == resource_id)
        .order_by(UsageSnapshot.polled_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()
    return quantize(total if total is not None else Decimal(0))


@router.get("/keys", response_model=list[KeySummaryOut])
def list_keys(scope: ScopeDep) -> list[KeySummaryOut]:
    session = scope.session
    q = (
        scope.issued_resources()
        .where(IssuedResource.provider == Provider.openrouter)
        .order_by(IssuedResource.created_at.desc())  # type: ignore[attr-defined]
    )
    out = []
    for res in session.exec(q).all():
        group, user = session.get(Group, res.group_id), session.get(User, res.user_id)
        out.append(KeySummaryOut(
            id=res.id,
            group_id=res.group_id,
            group_name=group.name if group else "",
            status=res.status.value,
            limit_usd=res.current_limit,
            usage_usd=_usage(session, res.id),
            created_at=res.created_at,
            user_id=res.user_id,
            user_name=user.name if user else "",
        ))
    return out


@router.post("/keys/{key_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(key_id: uuid.UUID, scope: ScopeDep) -> Response:
    session = scope.session
    res = scope.require_issued_resource(key_id)  # 404 outside scope
    if res.provider != Provider.openrouter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    scope.require_manage_group(res.group_id)  # 403: only the group's professor (D21)
    if res.status == ResourceStatus.disabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This key is already revoked")

    adapter = adapter_for_credential(session, res.credential_id)
    gone = False
    if adapter is not None:  # None: the credential was revoked, so the key is unusable anyway
        try:
            adapter.stop(res.external_ref)  # disable on OpenRouter
        except ResourceNotFound:
            gone = True
        except AdapterError as exc:
            raise _provider_error(exc) from None

    res.status = ResourceStatus.disabled
    session.add(res)
    write_audit(
        session, scope.me, "key.revoke", "issued_resource", res.id,
        {"group_id": res.group_id, "user_id": res.user_id, "by": "professor", "already_gone": gone},
    )
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)