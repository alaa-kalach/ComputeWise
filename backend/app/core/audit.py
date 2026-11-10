"""write_audit(): the one way everyone records an action in the audit log.

Call it INSIDE the same transaction as the action, before session.commit(), so the action
and its audit entry are saved together or not at all:

    request.status = RequestStatus.approved
    write_audit(session, me, "access_request.approve", "access_request", request.id,
                {"group_id": request.group_id, "provider": request.provider})
    session.commit()

The design requires an entry for every approval, limit change, key issue or revocation,
pod launch/stop, budget change and enforcement action.

Action names: "<thing>.<verb>", lowercase, e.g. access_request.approve, key.issue,
key.revoke, key.set_limit, pod.launch, pod.stop, budget.update, ledger.adjustment,
credential.grant, member.suspend. The audit_log table is append-only.
"""

import enum
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from app.models import AuditLog, User

if TYPE_CHECKING:
    from app.core.auth import CurrentUser

# Values under keys like these are never written to the log, even by mistake.
_SENSITIVE_KEY = re.compile(
    r"(secret|password|passwd|api_?key|access_token|refresh_token|authorization|private_key|management_key)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): REDACTED if _SENSITIVE_KEY.search(str(k)) else _json_safe(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if isinstance(value, enum.Enum):
        return _json_safe(value.value)
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)  # Decimal as a string keeps exact cents
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _actor_id(actor: "CurrentUser | User | uuid.UUID | None") -> uuid.UUID | None:
    if actor is None or isinstance(actor, uuid.UUID):
        return actor
    if isinstance(actor, User):
        return actor.id
    return actor.user.id  # CurrentUser


def write_audit(
    session: Session,
    actor: "CurrentUser | User | uuid.UUID | None",
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    details: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Add an audit entry to the current transaction (it is NOT committed here).

    actor: the logged-in user (CurrentUser), a User, a user id, or None for system jobs
    such as the sync job or the watchdog.
    """
    entry = AuditLog(
        actor_id=_actor_id(actor),
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        details=_json_safe(dict(details or {})),
    )
    session.add(entry)
    session.flush()  # surfaces errors (bad actor id etc.) at the call site
    return entry
