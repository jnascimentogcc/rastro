"""agent use: hook_active and the coverage of a session

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-21 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agent_sessions', sa.Column('hook_active', sa.Boolean(), nullable=True))
    op.create_table(
        'session_coverage',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('repository_id', sa.UUID(), nullable=False),
        sa.Column('session_ref', sa.Text(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('hook_active', sa.Boolean(), nullable=True),
        sa.Column('edited_ruled', sa.Integer(), nullable=False),
        sa.Column('consulted_first', sa.Integer(), nullable=False),
        sa.Column('files', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['repository_id'], ['repositories.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repository_id', 'session_ref'),
    )


def downgrade() -> None:
    op.drop_table('session_coverage')
    op.drop_column('agent_sessions', 'hook_active')
