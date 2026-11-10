"""Who is calling? Verify the Supabase login token and map it to a ComputeWise user and role.

The frontend logs in with Supabase Auth and sends the access token on every request:
    Authorization: Bearer <access_token>

Endpoints get the caller with a dependency:

    @router.get("/something")
    def something(me: CurrentUserDep): ...

    @router.post("/professor-only", dependencies=[Depends(require_role(Role.professor))])

Nobody signs themselves up: a user must already exist in the users table (added by a
department admin or a professor, or by the seed). On first login the Supabase account is
linked to that row by email, and from then on by auth_id.
"""

import enum
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlmodel import Session, select

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.db import get_session
from app.models import DepartmentAdmin, Professor, User


class Role(str, enum.Enum):
    """The design's three roles. Groups are not a role."""

    department_admin = "department_admin"
    professor = "professor"
    user = "user"


@dataclass(frozen=True)
class CurrentUser:
    user: User
    role: Role
    # department_admin: the department(s) they administer. professor: his department.
    department_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # professor only: professors.id (not users.id)
    professor_id: uuid.UUID | None = None

    @property
    def id(self) -> uuid.UUID:
        return self.user.id


_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


@lru_cache
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Supabase caches its JWKS for ~10 minutes; don't keep keys longer than that.
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def verify_token(token: str) -> dict[str, Any]:
    """Check signature, expiry, audience and issuer. Returns the token's claims.

    New Supabase projects sign tokens with asymmetric keys (ES256/RS256), checked against
    the project's public JWKS. Legacy projects use a shared HS256 secret
    (SUPABASE_JWT_SECRET), which the tests also use.
    """
    settings = get_settings()
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError:
        raise _unauthorized("Malformed token")

    key: Any
    if alg == "HS256":
        if not settings.supabase_jwt_secret:
            raise _unauthorized("This server does not accept HS256 tokens")
        key = settings.supabase_jwt_secret
    elif alg in ("ES256", "RS256"):
        if not settings.supabase_url:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "SUPABASE_URL is not configured")
        try:
            key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not reach Supabase to verify the token")
        except jwt.PyJWTError:
            raise _unauthorized("Unknown signing key")
    else:
        raise _unauthorized("Unsupported token algorithm")

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer if settings.supabase_url else None,
            options={"require": ["exp", "sub"], "verify_iss": bool(settings.supabase_url)},
            leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token expired")
    except jwt.PyJWTError:
        raise _unauthorized("Invalid token")

    if claims.get("is_anonymous"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Anonymous sessions are not allowed")
    return claims


def _resolve_user(session: Session, claims: dict[str, Any]) -> User:
    try:
        auth_id = uuid.UUID(str(claims["sub"]))
    except ValueError:
        raise _unauthorized("Invalid token subject")

    user = session.exec(select(User).where(User.auth_id == auth_id)).first()
    if user:
        return user

    # First login: link the Supabase account to the existing row with the same email.
    email = (claims.get("email") or "").strip().lower()
    if claims.get("user_metadata", {}).get("email_verified") is False:
        email = ""  # never link on an unconfirmed address
    if email:
        user = session.exec(
            select(User).where(func.lower(User.email) == email, User.auth_id.is_(None))  # type: ignore[union-attr]
        ).first()
    if not user:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "No ComputeWise account for this login. Ask your professor or department admin to add you.",
        )
    user.auth_id = auth_id
    session.add(user)
    write_audit(session, user, "user.link_login", "user", user.id, {"email": email})
    session.commit()
    session.refresh(user)
    return user


def load_current_user(session: Session, user: User) -> CurrentUser:
    """Work out the role of a known user. Department admin > professor > user."""
    admin_of = frozenset(
        session.exec(select(DepartmentAdmin.department_id).where(DepartmentAdmin.user_id == user.id)).all()
    )
    if admin_of:
        return CurrentUser(user=user, role=Role.department_admin, department_ids=admin_of)

    prof = session.exec(select(Professor).where(Professor.user_id == user.id)).first()
    if prof:
        return CurrentUser(
            user=user, role=Role.professor, department_ids=frozenset({prof.department_id}), professor_id=prof.id
        )
    return CurrentUser(user=user, role=Role.user)


def get_current_user(
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Not logged in")
    claims = verify_token(credentials.credentials)
    return load_current_user(session, _resolve_user(session, claims))


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_role(*roles: Role):
    """Dependency factory: 403 unless the caller has one of these roles."""

    def _check(me: CurrentUserDep) -> CurrentUser:
        if me.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed for your role")
        return me

    return _check

