"""Connects the Insight services to the real database (A6 audit, A7 ledger).

    svc = escalation_service(session, executors)   # escalations saved in Postgres + audit_log
    reports = report_service(session)               # reports computed from the real ledger

Both use the SAME session as the caller, so an escalation decision and its audit entry (or a
report and the rows it reads) belong to one transaction. The caller commits.
"""
from __future__ import annotations

import uuid
from datetime import timezone
from typing import Callable, Optional

from sqlmodel import Session, select

from app.core.audit import write_audit
from app.insight.db_store import DbStore
from app.insight.escalations import EscalationService
from app.insight.reports import ReportService
from app.models import Group, LedgerEntry, LedgerType, Professor, ProviderCredential, User


# ---------------------------------------------------------------- A6: the audit log
def audit_writer(session: Session) -> Callable:
    """The write_audit hook of EscalationService, backed by app.core.audit.write_audit.
    The agent and system jobs are not users, so they are recorded with no actor."""

    def write(actor_id, action, target_type, target_id, details=None):
        actor: Optional[uuid.UUID] = None
        try:
            actor = uuid.UUID(str(actor_id)) if actor_id else None
        except ValueError:  # "agent" and other non-user actors
            actor = None
        write_audit(session, actor, action, target_type, str(target_id), details)

    return write


def escalation_service(session: Session, executors: Optional[dict] = None, **kwargs) -> EscalationService:
    """Escalations stored in Postgres, every create / approve / reject / failure audited.
    `executors` maps an escalation kind to the function that carries it out (the real
    actions come from the access and budget code)."""
    return EscalationService(DbStore(session), executors=executors,
                             write_audit=audit_writer(session), **kwargs)


# ---------------------------------------------------------------- A7: the ledger
def spend_entries(session: Session) -> list[dict]:
    """Every spend row of the ledger as a report entry (see app/insight/reports.py).
    The ledger stores spend as a negative number; reports use the positive cost."""
    rows = session.exec(
        select(LedgerEntry, Group)
        .join(Group, Group.id == LedgerEntry.group_id)
        .where(LedgerEntry.type == LedgerType.spend)
    ).all()
    return [
        {
            "period": e.period,
            "professor_id": str(g.professor_id),
            "team_id": str(g.id),
            "provider": e.provider.value if e.provider else None,
            "credential_id": str(e.credential_id) if e.credential_id else None,
            "usd": float(-e.amount),
            "date": e.created_at.astimezone(timezone.utc).date().isoformat(),
        }
        for e, g in rows
    ]


def credential_owners(session: Session) -> dict[str, str]:
    """credential id -> "department" or "professor" (who owns it)."""
    return {str(c.id): c.owner_type.value for c in session.exec(select(ProviderCredential)).all()}


def professor_names(session: Session) -> dict[str, str]:
    rows = session.exec(select(Professor, User).join(User, User.id == Professor.user_id)).all()
    return {str(p.id): u.name for p, u in rows}


def report_service(session: Session) -> ReportService:
    """Reports computed from the real ledger, with the role rules of ReportService."""
    return ReportService(lambda: spend_entries(session), credential_owners(session), professor_names(session))
