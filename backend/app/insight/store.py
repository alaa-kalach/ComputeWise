"""In-memory store for escalations and notifications (used by tests and the
simulation). A database store will have the same methods."""
from __future__ import annotations

from typing import Optional

from app.insight.models import Escalation, Notification


class InMemoryStore:
    def __init__(self):
        self._escalations: dict[int, Escalation] = {}
        self._notifications: dict[int, Notification] = {}
        self._next_id = 1

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    # escalations
    def add_escalation(self, **fields) -> Escalation:
        e = Escalation(id=self._new_id(), **fields)
        self._escalations[e.id] = e
        return e

    def get_escalation(self, escalation_id: int) -> Optional[Escalation]:
        return self._escalations.get(escalation_id)

    def save_escalation(self, e: Escalation) -> None:
        """No-op: objects are already held in memory. The DB store writes back."""

    def escalations(self) -> list[Escalation]:
        return list(self._escalations.values())

    # notifications
    def add_notification(self, **fields) -> Notification:
        n = Notification(id=self._new_id(), **fields)
        self._notifications[n.id] = n
        return n

    def notifications(self) -> list[Notification]:
        return list(self._notifications.values())
