"""Top-ups and corrections. B10 adds the endpoints, with audit entries, on top.

Both are adjustment lines, never edits, because the ledger is append-only.
    top-up       positive: reopens a capped team before next month
    correction   either sign, e.g. undo a storage charge the sync counted twice

A top-up to a group must still respect the level above (policies.check_allocation),
so a professor can't top up past his own funding.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.budget.ledger import ZERO, adjustment_entry, money
from app.models import LedgerEntry, ScopeType


def top_up_entry(
    scope_type: ScopeType, scope_id: uuid.UUID, period: str, amount: Decimal, *,
    created_by: uuid.UUID, group_id: uuid.UUID | None = None, note: str | None = None,
) -> LedgerEntry:
    """A new, unsaved top-up line. It must be positive."""
    if money(amount) <= ZERO:
        raise ValueError("a top-up must be positive; use a correction to take money away")
    return adjustment_entry(scope_type, scope_id, period, amount, group_id=group_id,
                            created_by=created_by, note=note or f"Top-up ${money(amount)}")


def correction_entry(
    scope_type: ScopeType, scope_id: uuid.UUID, period: str, amount: Decimal, *,
    created_by: uuid.UUID | None, note: str, group_id: uuid.UUID | None = None,
) -> LedgerEntry:
    """A new, unsaved correction line. Either sign, but it always needs a reason."""
    if not note or not note.strip():
        raise ValueError("a correction needs a note saying why")
    return adjustment_entry(scope_type, scope_id, period, amount, group_id=group_id,
                            created_by=created_by, note=f"Correction: {note.strip()}")