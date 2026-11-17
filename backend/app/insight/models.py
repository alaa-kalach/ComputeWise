"""Escalation and notification records (A6).

Plain dataclasses with the same fields the database tables will have. When
Person 1's schema lands, these become SQLModel tables in backend/app/models/
and the InMemoryStore is replaced by a database store with the same methods.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Escalation statuses
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
FAILED = "failed"          # approved, but carrying out the action raised an error

# What the agent can ask for (the propose_* tools and escalate_to_department)
KINDS = ("propose_pod_stop", "propose_key_disable", "propose_budget_change",
         "escalate_to_department")

# Who decides
PROFESSOR = "professor"
DEPARTMENT = "department"

# Actor roles
ROLE_USER = "user"
ROLE_PROFESSOR = "professor"
ROLE_DEPARTMENT_ADMIN = "department_admin"


@dataclass
class Actor:
    """The person doing something. Only the fields that matter for permissions."""
    user_id: str
    role: str
    professor_id: Optional[str] = None     # set when role == professor
    department_id: Optional[str] = None    # set when role == department_admin


@dataclass
class Escalation:
    id: int
    kind: str
    target: str                      # pod id, key id, "team-b->team-a", or professor id
    reason: str                      # the four-line ISSUE / EVIDENCE / PROPOSED ACTION / IF IGNORED
    recipient_type: str              # PROFESSOR or DEPARTMENT
    recipient_id: str                # that professor's id, or the department's id
    created_at: datetime
    team_id: Optional[str] = None
    payload: dict = field(default_factory=dict)   # e.g. {"amount_usd": 10}
    status: str = PENDING
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: str = ""
    result: Optional[dict] = None


@dataclass
class Notification:
    id: int
    team_id: str
    message: str
    created_at: datetime
    level: str = "warning"
    read: bool = False
