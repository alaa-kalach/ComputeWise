"""professors.removed_at: the department removes a professor without losing history (A10)

His groups' ledger entries (append-only) reference his groups, so the professor row can't
be deleted once there is any history. Removal sets this instead; he loses the professor role.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("professors", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("professors", "removed_at")
