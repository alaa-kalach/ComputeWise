"""Provider credentials (A3).

    Department admin   adds and revokes DEPARTMENT credentials, grants them to professors
                       with an optional USD ceiling, changes or revokes those grants.
    Professor          adds and revokes his OWN credentials, and picks which credential he
                       uses per provider (his own or one granted to him): the binding.
    User               nothing here.

Rules (design + docs/decisions.md D11, D12, D13):
- The secret is Fernet-encrypted before it reaches the database and is never returned by any
  endpoint. Response models have no field that could hold it.
- One active credential per provider per professor (the binding's primary key).
- A department credential is billable, a professor's own is observed (D8).
- The grant ceiling caps what one professor spends through one department credential. It is
  separate from the professor's budget ceiling (A10, a budget policy).
- Every change writes an audit entry before commit.

For the rest of the backend (B3 key issuing, B4 pods, the sync job):

    cred = resolve_credential(session, group.professor_id, Provider.runpod)
    adapter = get_adapter(cred.provider, cred.secret)     # never log or store cred.secret
    ... IssuedResource(credential_id=cred.credential_id, ...)

resolve_credential raises CredentialUnavailable (a 409) when the professor has no usable
credential for that provider.

Revocation hook: revoking a credential or a grant calls every function registered with
@on_credential_revoked, inside the same transaction, before commit. Person 2's B6 cascade
(disable keys, stop pods) registers there; see CredentialRevoked.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.adapters import AdapterError, NotSupported, ProviderAdapter, ProviderUnavailable, get_adapter
from app.core.audit import write_audit
from app.core.crypto import decrypt, encrypt
from app.core.money import Money, NonNegativeMoney
from app.core.scoping import Scope, ScopeDep
from app.models import (
    CredentialGrant,
    CredentialStatus,
    GrantStatus,
    OwnerType,
    Professor,
    ProfessorProviderBinding,
    Provider,
    ProviderCredential,
    User,
)

if TYPE_CHECKING:
    from app.core.auth import CurrentUser

router = APIRouter(tags=["credentials"])


# ====================================================================== API models
class GrantOut(BaseModel):
    id: uuid.UUID
    credential_id: uuid.UUID
    professor_id: uuid.UUID
    professor_name: str
    ceiling_usd: Money | None
    status: GrantStatus


class CredentialOut(BaseModel):
    """Metadata only. There is deliberately no field that could hold the secret."""

    id: uuid.UUID
    owner_type: OwnerType
    owner_id: uuid.UUID
    provider: Provider
    label: str
    status: CredentialStatus
    created_at: datetime
    grants: list[GrantOut]


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: OwnerType
    provider: Provider
    label: str = Field(min_length=1, max_length=200)
    # SecretStr keeps the key out of reprs and logs.
    secret: SecretStr = Field(min_length=1, max_length=4096)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    professor_id: uuid.UUID
    ceiling_usd: NonNegativeMoney | None = None


class GrantPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceiling_usd: NonNegativeMoney | None


class BindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: uuid.UUID


class BindingOut(BaseModel):
    provider: Provider
    credential_id: uuid.UUID
    credential_label: str
    is_department: bool  # True = billable to the department


# ====================================================================== revocation hook
@dataclass(frozen=True)
class CredentialRevoked:
    """What was cut off.

    professor_id is None when the whole credential was revoked (everyone using it loses it),
    or a professor's id when only his grant was revoked (only his groups lose it).
    """

    credential_id: uuid.UUID
    provider: Provider
    professor_id: uuid.UUID | None
    action: str  # "credential.revoke" or "grant.revoke"


# hook(session, actor, event). Must not commit. Should not raise on ProviderUnavailable:
# leave the resource for the next sync, which also cuts off everything under a revoked
# credential (B6).
RevokeHook = Callable[[Session, "CurrentUser | None", CredentialRevoked], None]
_REVOKE_HOOKS: list[RevokeHook] = []


def on_credential_revoked(hook: RevokeHook) -> RevokeHook:
    """Register a function to run when a credential or grant is revoked (decorator)."""
    if hook not in _REVOKE_HOOKS:
        _REVOKE_HOOKS.append(hook)
    return hook


def _run_revoke_hooks(session: Session, actor: CurrentUser | None, event: CredentialRevoked) -> None:
    for hook in list(_REVOKE_HOOKS):
        hook(session, actor, event)


# ====================================================================== for other modules
class CredentialUnavailable(HTTPException):
    """The professor has no usable credential for this provider (none bound, revoked, or
    the department grant was revoked). An HTTPException (409), so endpoints can let it
    propagate; jobs can catch it like any exception."""

    def __init__(self, provider: Provider, why: str) -> None:
        super().__init__(
            status.HTTP_409_CONFLICT,
            f"No usable {provider.value} credential: {why}. "
            "The professor must add his own or get a grant from the department.",
        )
        self.provider = provider


@dataclass(frozen=True)
class ResolvedCredential:
    credential_id: uuid.UUID
    provider: Provider
    is_department: bool  # True = billable
    grant_ceiling_usd: Decimal | None  # department credentials only; None = no ceiling
    secret: str = field(repr=False)  # decrypted. Pass to get_adapter; never log, store or return it.


def resolve_credential(session: Session, professor_id: uuid.UUID, provider: Provider) -> ResolvedCredential:
    """The credential a professor's groups use for a provider, with its decrypted secret."""
    binding = session.exec(
        select(ProfessorProviderBinding).where(
            ProfessorProviderBinding.professor_id == professor_id,
            ProfessorProviderBinding.provider == provider,
        )
    ).first()
    if binding is None:
        raise CredentialUnavailable(provider, "none is bound")
    cred = session.get(ProviderCredential, binding.credential_id)
    if cred is None or cred.status != CredentialStatus.active:
        raise CredentialUnavailable(provider, "the bound credential was revoked")

    ceiling: Decimal | None = None
    is_department = cred.owner_type == OwnerType.department
    if is_department:
        grant = _active_grant(session, cred.id, professor_id)
        if grant is None:
            raise CredentialUnavailable(provider, "the department grant was revoked")
        ceiling = grant.ceiling_usd
    elif cred.owner_id != professor_id:
        raise CredentialUnavailable(provider, "the bound credential belongs to someone else")

    return ResolvedCredential(
        credential_id=cred.id,
        provider=cred.provider,
        is_department=is_department,
        grant_ceiling_usd=ceiling,
        secret=decrypt(cred.secret_encrypted),
    )


def adapter_for_credential(session: Session, credential_id: uuid.UUID) -> ProviderAdapter | None:
    """Adapter for the credential a resource was ISSUED under (which may differ from the
    professor's current binding). None if that credential was revoked: its keys and pods
    are the revoke cascade's job."""
    cred = session.get(ProviderCredential, credential_id)
    if cred is None or cred.status != CredentialStatus.active:
        return None
    return get_adapter(cred.provider, decrypt(cred.secret_encrypted))


# ====================================================================== internals
PROBE_REF = "computewise-credential-check"


def verify_secret(provider: Provider, secret: str) -> None:
    """One read-only call to check the provider accepts the key (contract: 422 if not).

    Asks for the usage of a resource that doesn't exist: a valid key gets "gone" back, a bad
    key gets 401. Works the same for every adapter, so no provider-specific code here.
    """
    try:
        adapter = get_adapter(provider, secret)
    except NotSupported:
        return  # the real adapter for this provider isn't built yet; nothing to check against
    except ValueError:
        raise HTTPException(422, f"{provider.value} key is empty") from None
    try:
        adapter.fetch_usage([PROBE_REF])
    except ProviderUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not reach {provider.value} to check the key. Try again in a minute.",
        ) from None
    except AdapterError:
        # Never echo the provider's message: keep the response free of anything key-related.
        raise HTTPException(
            422,
            f"{provider.value} rejected this key. Check that it is a "
            + ("management key" if provider is Provider.openrouter else "RunPod API key")
            + " and that it is still active.",
        ) from None


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def _admin_of(scope: Scope, cred: ProviderCredential) -> bool:
    """Is the caller a department admin of the department that owns this credential?"""
    return (
        scope.me.is_admin
        and cred.owner_type == OwnerType.department
        and cred.owner_id in scope.me.admin_department_ids
    )


def _owner_of(scope: Scope, cred: ProviderCredential) -> bool:
    if cred.owner_type == OwnerType.department:
        return _admin_of(scope, cred)
    return scope.me.is_professor and cred.owner_id == scope.me.professor_id


def _require_grant_admin(scope: Scope, cred: ProviderCredential) -> None:
    if cred.owner_type != OwnerType.department:
        raise _forbidden("Only department credentials can be granted")
    if not _admin_of(scope, cred):
        raise _forbidden("Only the department admin manages grants of department credentials")


def _active_grant(session: Session, credential_id: uuid.UUID, professor_id: uuid.UUID) -> CredentialGrant | None:
    return session.exec(
        select(CredentialGrant).where(
            CredentialGrant.credential_id == credential_id,
            CredentialGrant.professor_id == professor_id,
            CredentialGrant.status == GrantStatus.active,
        )
    ).first()


def _grant_out(grant: CredentialGrant, professor_name: str) -> GrantOut:
    return GrantOut(
        id=grant.id,
        credential_id=grant.credential_id,
        professor_id=grant.professor_id,
        professor_name=professor_name,
        ceiling_usd=grant.ceiling_usd,
        status=grant.status,
    )


def _professor_name(session: Session, professor_id: uuid.UUID) -> str:
    name = session.exec(
        select(User.name).join(Professor, Professor.user_id == User.id).where(Professor.id == professor_id)
    ).first()
    return name or ""


def _credential_out(scope: Scope, creds: list[ProviderCredential]) -> list[CredentialOut]:
    """Attach grants: the department admin sees every grant of his department's credentials;
    a professor sees only his own grant."""
    dept_ids = [c.id for c in creds if c.owner_type == OwnerType.department]
    grants: dict[uuid.UUID, list[GrantOut]] = {c.id: [] for c in creds}
    if dept_ids:
        rows = scope.session.exec(
            select(CredentialGrant, User.name)
            .join(Professor, Professor.id == CredentialGrant.professor_id)
            .join(User, User.id == Professor.user_id)
            .where(CredentialGrant.credential_id.in_(dept_ids))  # type: ignore[attr-defined]
            .order_by(User.name, CredentialGrant.created_at)
        ).all()
        by_id = {c.id: c for c in creds}
        for grant, name in rows:
            cred = by_id[grant.credential_id]
            if _admin_of(scope, cred) or grant.professor_id == scope.me.professor_id:
                grants[grant.credential_id].append(_grant_out(grant, name))
    return [
        CredentialOut(
            id=c.id,
            owner_type=c.owner_type,
            owner_id=c.owner_id,
            provider=c.provider,
            label=c.label,
            status=c.status,
            created_at=c.created_at,
            grants=grants[c.id],
        )
        for c in creds
    ]


def _binding_out(session: Session, binding: ProfessorProviderBinding) -> BindingOut:
    cred = session.get(ProviderCredential, binding.credential_id)
    assert cred is not None
    return BindingOut(
        provider=binding.provider,
        credential_id=cred.id,
        credential_label=cred.label,
        is_department=cred.owner_type == OwnerType.department,
    )


def _get_binding(session: Session, professor_id: uuid.UUID, provider: Provider) -> ProfessorProviderBinding | None:
    return session.exec(
        select(ProfessorProviderBinding).where(
            ProfessorProviderBinding.professor_id == professor_id,
            ProfessorProviderBinding.provider == provider,
        )
    ).first()


def _bind_if_unbound(
    session: Session, actor: CurrentUser, professor_id: uuid.UUID, cred: ProviderCredential
) -> None:
    """A professor with no credential for this provider starts using the new one right away."""
    if _get_binding(session, professor_id, cred.provider) is not None:
        return
    session.add(ProfessorProviderBinding(professor_id=professor_id, provider=cred.provider, credential_id=cred.id))
    write_audit(
        session, actor, "binding.set", "professor", professor_id,
        {"provider": cred.provider, "credential_id": cred.id, "previous_credential_id": None, "automatic": True},
    )


def _unbind(session: Session, credential_id: uuid.UUID, professor_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Remove bindings to a credential (for one professor, or everyone). Returns who lost it."""
    q = select(ProfessorProviderBinding).where(ProfessorProviderBinding.credential_id == credential_id)
    if professor_id is not None:
        q = q.where(ProfessorProviderBinding.professor_id == professor_id)
    removed = []
    for b in session.exec(q).all():
        removed.append(b.professor_id)
        session.delete(b)
    return removed


def _require_grant(scope: Scope, cred: ProviderCredential, grant_id: uuid.UUID) -> CredentialGrant:
    grant = scope.session.get(CredentialGrant, grant_id)
    if grant is None or grant.credential_id != cred.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grant not found")
    return grant


# ====================================================================== credentials
@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(scope: ScopeDep) -> list[CredentialOut]:
    """Admin: department credentials and his professors' own. Professor: his own plus
    granted ones. User: none."""
    q = scope.credentials().order_by(ProviderCredential.created_at, ProviderCredential.label)
    return _credential_out(scope, list(scope.session.exec(q).all()))


@router.post("/credentials", response_model=CredentialOut, status_code=status.HTTP_201_CREATED)
def add_credential(body: CredentialIn, scope: ScopeDep) -> CredentialOut:
    me, session = scope.me, scope.session
    if body.owner == OwnerType.department:
        if not me.is_admin:
            raise _forbidden("Only a department admin adds department credentials")
        if len(me.admin_department_ids) != 1:
            raise HTTPException(422, "You administer several departments")
        (owner_id,) = me.admin_department_ids
    else:
        if not me.is_professor or me.professor_id is None:
            raise _forbidden("Only a professor adds his own credentials")
        owner_id = me.professor_id

    secret = body.secret.get_secret_value().strip()
    if not secret:
        raise HTTPException(422, "The key is empty")
    verify_secret(body.provider, secret)

    cred = ProviderCredential(
        owner_type=body.owner,
        owner_id=owner_id,
        provider=body.provider,
        secret_encrypted=encrypt(secret),
        label=body.label.strip(),
        created_by=me.id,
    )
    session.add(cred)
    session.flush()
    write_audit(
        session, me, "credential.add", "credential", cred.id,
        {"owner_type": cred.owner_type, "owner_id": owner_id, "provider": cred.provider, "label": cred.label},
    )
    if body.owner == OwnerType.professor:
        _bind_if_unbound(session, me, owner_id, cred)
    session.commit()
    session.refresh(cred)
    return _credential_out(scope, [cred])[0]


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_credential(credential_id: uuid.UUID, scope: ScopeDep) -> Response:
    """Revoke for everyone: its grants end, professors using it lose their binding, and the
    revoke hooks cut off the keys and pods issued under it. Revoking twice is a no-op."""
    session = scope.session
    cred = scope.require_credential(credential_id)
    if not _owner_of(scope, cred):
        raise _forbidden("Only the credential's owner can revoke it")
    if cred.status == CredentialStatus.revoked:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    cred.status = CredentialStatus.revoked
    session.add(cred)
    grants = session.exec(
        select(CredentialGrant).where(
            CredentialGrant.credential_id == cred.id, CredentialGrant.status == GrantStatus.active
        )
    ).all()
    for g in grants:
        g.status = GrantStatus.revoked
        session.add(g)
    unbound = _unbind(session, cred.id)
    write_audit(
        session, scope.me, "credential.revoke", "credential", cred.id,
        {
            "provider": cred.provider,
            "owner_type": cred.owner_type,
            "owner_id": cred.owner_id,
            "grants_revoked": [g.id for g in grants],
            "unbound_professor_ids": unbound,
        },
    )
    _run_revoke_hooks(session, scope.me, CredentialRevoked(cred.id, cred.provider, None, "credential.revoke"))
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ====================================================================== grants
@router.post("/credentials/{credential_id}/grants", response_model=GrantOut, status_code=status.HTTP_201_CREATED)
def grant_credential(credential_id: uuid.UUID, body: GrantIn, scope: ScopeDep) -> GrantOut:
    session = scope.session
    cred = scope.require_credential(credential_id)
    _require_grant_admin(scope, cred)
    if cred.status != CredentialStatus.active:
        raise _conflict("This credential is revoked")
    prof = scope.require_professor(body.professor_id)
    if prof.department_id != cred.owner_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professor not found")
    if _active_grant(session, cred.id, prof.id) is not None:
        raise _conflict("Already granted to this professor")

    grant = CredentialGrant(credential_id=cred.id, professor_id=prof.id, ceiling_usd=body.ceiling_usd)
    session.add(grant)
    try:
        session.flush()
    except IntegrityError:  # two admins granting at the same moment
        session.rollback()
        raise _conflict("Already granted to this professor") from None
    write_audit(
        session, scope.me, "credential.grant", "credential", cred.id,
        {"grant_id": grant.id, "professor_id": prof.id, "provider": cred.provider, "ceiling_usd": grant.ceiling_usd},
    )
    _bind_if_unbound(session, scope.me, prof.id, cred)
    session.commit()
    session.refresh(grant)
    return _grant_out(grant, _professor_name(session, prof.id))


@router.patch("/credentials/{credential_id}/grants/{grant_id}", response_model=GrantOut)
def update_grant(credential_id: uuid.UUID, grant_id: uuid.UUID, body: GrantPatch, scope: ScopeDep) -> GrantOut:
    """Change a grant's ceiling (null = no ceiling). Enforcement against the new ceiling is
    the watchdog's job (B6); lowering it below what was spent doesn't undo spend."""
    session = scope.session
    cred = scope.require_credential(credential_id)
    _require_grant_admin(scope, cred)
    grant = _require_grant(scope, cred, grant_id)
    if grant.status != GrantStatus.active:
        raise _conflict("This grant is revoked")

    old = grant.ceiling_usd
    if old != body.ceiling_usd:
        grant.ceiling_usd = body.ceiling_usd
        session.add(grant)
        write_audit(
            session, scope.me, "grant.update", "credential_grant", grant.id,
            {"credential_id": cred.id, "professor_id": grant.professor_id, "old_ceiling_usd": old,
             "ceiling_usd": body.ceiling_usd},
        )
        session.commit()
        session.refresh(grant)
    return _grant_out(grant, _professor_name(session, grant.professor_id))


@router.delete("/credentials/{credential_id}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_grant(credential_id: uuid.UUID, grant_id: uuid.UUID, scope: ScopeDep) -> Response:
    """Same cascade as revoking the credential, for this one professor."""
    session = scope.session
    cred = scope.require_credential(credential_id)
    _require_grant_admin(scope, cred)
    grant = _require_grant(scope, cred, grant_id)
    if grant.status == GrantStatus.revoked:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    grant.status = GrantStatus.revoked
    session.add(grant)
    unbound = _unbind(session, cred.id, grant.professor_id)
    write_audit(
        session, scope.me, "grant.revoke", "credential_grant", grant.id,
        {"credential_id": cred.id, "professor_id": grant.professor_id, "provider": cred.provider,
         "unbound": bool(unbound)},
    )
    _run_revoke_hooks(
        session, scope.me, CredentialRevoked(cred.id, cred.provider, grant.professor_id, "grant.revoke")
    )
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ====================================================================== bindings
def _require_professor_role(scope: Scope) -> uuid.UUID:
    if not scope.me.is_professor or scope.me.professor_id is None:
        raise _forbidden("Only professors choose which credential they use")
    return scope.me.professor_id


@router.get("/bindings", response_model=list[BindingOut])
def list_bindings(scope: ScopeDep) -> list[BindingOut]:
    professor_id = _require_professor_role(scope)
    rows = scope.session.exec(
        select(ProfessorProviderBinding)
        .where(ProfessorProviderBinding.professor_id == professor_id)
        .order_by(ProfessorProviderBinding.provider)
    ).all()
    return [_binding_out(scope.session, b) for b in rows]


@router.put("/bindings/{provider}", response_model=BindingOut)
def set_binding(provider: Provider, body: BindingIn, scope: ScopeDep) -> BindingOut:
    """Choose the credential this professor's groups use for a provider: his own or one
    granted to him. Keys and pods already issued keep the credential they were issued under."""
    session = scope.session
    professor_id = _require_professor_role(scope)
    cred = scope.require_credential(body.credential_id)

    own = cred.owner_type == OwnerType.professor and cred.owner_id == professor_id
    granted = cred.owner_type == OwnerType.department and _active_grant(session, cred.id, professor_id) is not None
    if not (own or granted):
        raise _forbidden("You can only use your own credential or one the department granted to you")
    if cred.provider != provider:
        raise HTTPException(422, f"This credential is for {cred.provider.value}")
    if cred.status != CredentialStatus.active:
        raise HTTPException(422, "This credential is revoked")

    binding = _get_binding(session, professor_id, provider)
    previous = binding.credential_id if binding else None
    if previous != cred.id:
        if binding is None:
            binding = ProfessorProviderBinding(professor_id=professor_id, provider=provider, credential_id=cred.id)
        else:
            binding.credential_id = cred.id
        session.add(binding)
        write_audit(
            session, scope.me, "binding.set", "professor", professor_id,
            {"provider": provider, "credential_id": cred.id, "previous_credential_id": previous},
        )
        session.commit()
        session.refresh(binding)
    return _binding_out(session, binding)


__all__ = [
    "CredentialRevoked",
    "adapter_for_credential",
    "CredentialUnavailable",
    "ResolvedCredential",
    "on_credential_revoked",
    "resolve_credential",
    "router",
    "verify_secret",
]
