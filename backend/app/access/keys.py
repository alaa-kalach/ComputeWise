"""B3: a student gets his OpenRouter key, once.

POST /keys (proposed in the contract, owner Person 3):
  - only after the professor approved his OpenRouter request (403 otherwise)
  - the key is created on OpenRouter with a credit limit = what he may still spend (D9:
    the team's remaining, or his own member limit if lower)
  - the plaintext key is returned in THIS response only. We store the key's hash, never the key.
  - asking again returns 409.
"""

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from app.access.credentials import resolve_credential
from app.access.pods import available_budget
from app.access.requests import require_approved_access
from app.adapters import AdapterError, IssueRequest, ProviderUnavailable, get_adapter
from app.core.audit import write_audit
from app.core.money import Money, quantize
from app.core.scoping import ScopeDep
from app.models import IssuedResource, Provider, ResourceStatus

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
