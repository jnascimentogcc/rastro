"""encrypt existing agent_sessions.excerpts

The column stays jsonb; encrypted values are {"v": 1, "ct": ...} envelopes. Existing plaintext
rows are encrypted with the primary key of EXCERPTS_ENCRYPTION_KEYS. Without a key the data
migration does nothing (plaintext stays readable); run `python -m rastro_db.crypto reencrypt`
once a key is configured.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 12:01:00
"""
from alembic import op

from rastro_db.crypto import encrypt_existing_rows


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    encrypt_existing_rows(op.get_bind())


def downgrade() -> None:
    # Not reversible without the key; decrypting would defeat the purpose. Readers keep
    # working because envelopes are decrypted by the application.
    pass
