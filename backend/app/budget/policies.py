"""Budget policy rules. A4 adds the database endpoints on top.

The one hard rule: allocations at any level never exceed the level above. Checked here in
the backend, not just in the UI. Levels:

    department -> professor   the professor's ceiling, or none when he is "tracking only"
    professor  -> groups      his group allocations together <= his funding this month
    group      -> users       user sub-budgets in the group together <= the group's funding
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from app.budget.ledger import ZERO, money


class OverAllocation(ValueError):
    """An allocation would exceed the level above. Endpoints turn it into HTTP 409."""

    def __init__(self, ceiling: Decimal, already: Decimal, requested: Decimal) -> None:
        self.ceiling, self.already, self.requested = ceiling, already, requested
        self.available = max(ZERO, ceiling - already)
        super().__init__(
            f"allocation of ${requested} exceeds what is left above it: "
            f"${self.available} of ${ceiling} (already allocated ${already})"
        )


def check_allocation(ceiling: Decimal | None, already_allocated: Decimal, requested: Decimal) -> None:
    """Raise OverAllocation if `requested` on top of `already_allocated` would pass the
    ceiling. ceiling=None means tracking only: no limit to enforce."""
    requested, already = money(requested), money(already_allocated)
    if requested < ZERO:
        raise ValueError("allocation must not be negative")
    if ceiling is None:
        return
    ceiling = money(ceiling)
    if already + requested > ceiling:
        raise OverAllocation(ceiling, already, requested)


def check_children_fit(ceiling: Decimal | None, child_amounts: Iterable[Decimal]) -> None:
    """For when a professor sets several group budgets at once: all of them together
    must fit under the ceiling."""
    amounts = [money(a) for a in child_amounts]
    if any(a < ZERO for a in amounts):
        raise ValueError("allocation must not be negative")
    if ceiling is None:
        return
    total = money(sum(amounts, ZERO))
    if total > money(ceiling):
        raise OverAllocation(money(ceiling), ZERO, total)