"""repository lifecycle status and webhook delivery ids

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('repositories', sa.Column('status', sa.Text(), server_default='active', nullable=False))
    op.add_column('repositories', sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('webhook_deliveries',
    sa.Column('delivery_id', sa.Text(), nullable=False),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('delivery_id')
    )


def downgrade() -> None:
    op.drop_table('webhook_deliveries')
    op.drop_column('repositories', 'status_changed_at')
    op.drop_column('repositories', 'status')
