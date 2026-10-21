"""daily snapshots of the health report

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-22 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'health_snapshots',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('repository_id', sa.UUID(), nullable=False),
        sa.Column('taken_on', sa.Date(), nullable=False),
        sa.Column('taken_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('window_days', sa.Integer(), nullable=False),
        sa.Column('definitions_version', sa.Integer(), nullable=False),
        sa.Column('head_sha', sa.Text(), nullable=True),
        sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['repository_id'], ['repositories.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repository_id', 'taken_on'),
    )


def downgrade() -> None:
    op.drop_table('health_snapshots')
