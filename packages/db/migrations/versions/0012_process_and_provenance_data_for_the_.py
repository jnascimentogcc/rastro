"""process and provenance data for the health report

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('commits', sa.Column('merged_pr_number', sa.Integer(), nullable=True))
    op.add_column('rule_proposals', sa.Column('pr_author', sa.Text(), nullable=True))
    op.add_column('rule_reviews', sa.Column('reviewer_is_pr_author', sa.Boolean(), nullable=True))
    # The commits that already have proposals are known members of their pull request. The merge or
    # squash commit and commits of PRs without proposals stay NULL (reported as unknown, never guessed).
    op.execute(
        """
        UPDATE commits c SET merged_pr_number = p.pr_number
        FROM (
            SELECT repository_id, commit_sha, max(pr_number) AS pr_number
            FROM rule_proposals GROUP BY repository_id, commit_sha
        ) p
        WHERE c.repository_id = p.repository_id AND c.sha = p.commit_sha AND c.merged_pr_number IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column('rule_reviews', 'reviewer_is_pr_author')
    op.drop_column('rule_proposals', 'pr_author')
    op.drop_column('commits', 'merged_pr_number')
