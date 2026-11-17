"""Notifications (A6): warnings sent to a team's members by the agent."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

DEFAULT_COOLDOWN_HOURS = 24


class NotificationService:
    def __init__(self, store, now: Callable[[], datetime] = datetime.utcnow,
                 cooldown_hours: int = DEFAULT_COOLDOWN_HOURS):
        self.store = store
        self.now = now
        self.cooldown = timedelta(hours=cooldown_hours)

    def warn(self, team_id: str, message: str):
        """Returns (notification, created). One warning per team per cooldown,
        so the agent cannot spam the same team."""
        cutoff = self.now() - self.cooldown
        for n in self.store.notifications():
            if n.team_id == team_id and n.created_at >= cutoff:
                return n, False
        n = self.store.add_notification(team_id=team_id, message=message,
                                        created_at=self.now())
        return n, True

    def for_team(self, team_id: str):
        items = [n for n in self.store.notifications() if n.team_id == team_id]
        return sorted(items, key=lambda n: n.created_at, reverse=True)
