"""the code a person looked at when confirming a rule

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('rule_reviews', sa.Column('anchor_hashes', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('rule_reviews', 'anchor_hashes')
