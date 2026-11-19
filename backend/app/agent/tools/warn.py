"""warn_user (Person 2, Week 3): the agent warns the members of one team.

A warning changes nothing: it is a notification the team sees on its dashboard. It goes
through Person 3's NotificationService, so the cooldown applies: one warning per team per
24 hours, counting the watchdog's own 80% / 100% notices. That is how the agent does not
repeat a rule-based alert that was just sent.

The evidence gate (app/agent/gates.py) has already checked that the message names the top
spend drivers before this function runs.
"""
from __future__ import annotations

import uuid

from sqlmodel import Session

from app.core.audit import write_audit
from app.insight.db_store import DbStore
from app.insight.notifications import NotificationService
from app.models import Group

MAX_MESSAGE = 2000


def tool_warn_user(session: Session, team_id: str, message: str) -> dict:
    """Agent tool: send one warning notification to a team."""
    try:
        gid = uuid.UUID(str(team_id))
    except ValueError:
        gid = None
    group = session.get(Group, gid) if gid else None
    if group is None:
        return {"status": "failed", "detail": f"unknown team: {team_id}"}
    text = (message or "").strip()
    if not text:
        return {"status": "failed", "detail": "the warning is empty. Write what drove the spend and the projected run-out."}
    text = text[:MAX_MESSAGE]

    note, created = NotificationService(DbStore(session)).warn(str(gid), text)
    if not created:
        # Not a failure: the team was told recently. "ok" so the agent does not retry it.
        return {
            "status": "ok",
            "sent": False,
            "detail": "not sent: this team already got a warning in the last 24 hours. Do not warn again.",
            "notification_id": note.id,
        }

    write_audit(session, None, "notification.warn", "group", gid,
                {"by": "agent", "notification_id": note.id})
    session.commit()
    return {"status": "ok", "sent": True, "detail": "warning sent to the team", "notification_id": note.id}