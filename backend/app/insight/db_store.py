"""Database store for escalations and notifications (A6).

Same methods as InMemoryStore, so EscalationService and NotificationService work
unchanged. Ids are UUIDs in the database and strings to the services. Datetimes
are naive UTC to the services (they use datetime.utcnow) and timezone-aware in the DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.insight import models as m
from app.models import Escalation as EscalationRow
from app.models import Notification as NotificationRow


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _uuid(value) -> Optional[uuid.UUID]:
    return None if value is None else uuid.UUID(str(value))


def _s(value) -> Optional[str]:
    return None if value is None else str(value)


class DbStore:
    def __init__(self, session: Session):
        self.session = session

    # ---- escalations
    @staticmethod
    def _to_escalation(r: EscalationRow) -> m.Escalation:
        return m.Escalation(
            id=str(r.id), kind=r.kind, target=r.target, reason=r.reason,
            recipient_type=r.recipient_type, recipient_id=str(r.recipient_id),
            created_at=_naive(r.created_at), team_id=_s(r.group_id),
            payload=dict(r.payload or {}), status=r.status, decided_by=_s(r.decided_by),
            decided_at=_naive(r.decided_at), note=r.note or "", result=r.result)

    def add_escalation(self, **f) -> m.Escalation:
        row = EscalationRow(
            kind=f["kind"], target=f["target"], reason=f["reason"],
            recipient_type=f["recipient_type"], recipient_id=_uuid(f["recipient_id"]),
            group_id=_uuid(f.get("team_id")), payload=f.get("payload") or {},
            created_at=_aware(f["created_at"]))
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return self._to_escalation(row)

    def get_escalation(self, escalation_id) -> Optional[m.Escalation]:
        try:
            row = self.session.get(EscalationRow, _uuid(escalation_id))
        except ValueError:
            return None
        return self._to_escalation(row) if row else None

    def escalations(self) -> list[m.Escalation]:
        rows = self.session.exec(select(EscalationRow)).all()
        return [self._to_escalation(r) for r in rows]

    def save_escalation(self, e: m.Escalation) -> None:
        """Write back the fields a decision changes."""
        row = self.session.get(EscalationRow, _uuid(e.id))
        row.status, row.note, row.result = e.status, e.note, e.result
        row.decided_by, row.decided_at = _uuid(e.decided_by), _aware(e.decided_at)
        self.session.add(row)
        self.session.commit()

    # ---- notifications
    @staticmethod
    def _to_notification(r: NotificationRow) -> m.Notification:
        return m.Notification(id=str(r.id), team_id=str(r.group_id), message=r.message,
                              created_at=_naive(r.created_at), level=r.level, read=r.read)

    def add_notification(self, **f) -> m.Notification:
        row = NotificationRow(group_id=_uuid(f["team_id"]), message=f["message"],
                              level=f.get("level", "warning"),
                              created_at=_aware(f["created_at"]))
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return self._to_notification(row)

    def notifications(self) -> list[m.Notification]:
        rows = self.session.exec(select(NotificationRow)).all()
        return [self._to_notification(r) for r in rows]
