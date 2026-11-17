"""escalations and notifications (agent proposals and team warnings)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('escalations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('target', sa.String(length=200), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('recipient_type', sa.String(length=20), nullable=False),
        sa.Column('recipient_id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('decided_by', sa.Uuid(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('note', sa.Text(), server_default='', nullable=False),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("recipient_type IN ('professor', 'department')", name=op.f('ck_escalations_recipient_type_valid')),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'failed')", name=op.f('ck_escalations_status_valid')),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name=op.f('fk_escalations_group_id_groups')),
        sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_escalations_decided_by_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_escalations')),
    )
    op.create_index(op.f('ix_escalations_recipient_type_recipient_id_status'), 'escalations', ['recipient_type', 'recipient_id', 'status'], unique=False)
    op.create_index(op.f('ix_escalations_kind_target_recipient_id_created_at'), 'escalations', ['kind', 'target', 'recipient_id', 'created_at'], unique=False)

    op.create_table('notifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('level', sa.String(length=20), nullable=False),
        sa.Column('read', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name=op.f('fk_notifications_group_id_groups'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    )
    op.create_index(op.f('ix_notifications_group_id_created_at'), 'notifications', ['group_id', 'created_at'], unique=False)

    # Same as the initial migration: RLS on, no policies, so Supabase's public API sees nothing.
    for table in ("escalations", "notifications"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    op.drop_index(op.f('ix_notifications_group_id_created_at'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_escalations_kind_target_recipient_id_created_at'), table_name='escalations')
    op.drop_index(op.f('ix_escalations_recipient_type_recipient_id_status'), table_name='escalations')
    op.drop_table('escalations')
