"""access_requests.reason: the professor's reason when rejecting a request (A5)

The contract's AccessRequest has a `reason` field; the initial schema had no column for it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("access_requests", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("access_requests", "reason")
