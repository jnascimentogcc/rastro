"""SQLAlchemy models. Reference DDL: docs/spec-rastro-mvp.md section 6."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rastro_db.base import Base
from rastro_db.config import EMBEDDING_DIMENSIONS
from rastro_db.crypto import EncryptedExcerpts


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("api_token_hash", name="uq_repositories_api_token_hash"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 of the repository API token used by the CLI (the token itself is never stored).
    api_token_hash: Mapped[str | None] = mapped_column(Text)
    # active | suspended | uninstalled. Anything but active is not analysed; the data is kept
    # (retention choice in the README) and a reinstall or unsuspend reactivates it.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class WebhookDelivery(Base):
    """GitHub delivery ids already processed, so a replayed webhook is ignored."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = _now()


class Commit(Base):
    __tablename__ = "commits"
    __table_args__ = (UniqueConstraint("repository_id", "sha"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    sha: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # analyzed | analysis_failed | skipped_no_session | skipped_budget | skipped_quota |
    # awaiting_merge (has a session but is not on the default branch yet) | NULL (not analyzed yet)
    analysis_status: Mapped[str | None] = mapped_column(Text)
    # Set by the push webhook: True once the commit is part of the default branch. NULL = unknown
    # (rows from before the flag, or created by tools); only an explicit False defers extraction.
    on_default_branch: Mapped[bool | None] = mapped_column(Boolean)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "repository_id", "commit_sha", "session_ref", name="uq_agent_sessions_commit_session"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    agent: Mapped[str] = mapped_column(Text, nullable=False, server_default="claude-code")
    session_ref: Mapped[str | None] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(Text)
    git_branch: Mapped[str | None] = mapped_column(Text)
    # encrypted at rest (rastro_db.crypto); readers still see a list of dicts
    excerpts: Mapped[list[dict[str, Any]]] = mapped_column(EncryptedExcerpts, nullable=False)
    captured_at: Mapped[datetime] = _now()


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("repository_id", "file_path", "qualified_name"),
        Index(
            "ix_symbols_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # function | method | class
    body_hash: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    last_seen_sha: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")


class SymbolCall(Base):
    __tablename__ = "symbol_calls"

    caller_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), primary_key=True
    )
    callee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), primary_key=True
    )


class Rule(Base):
    __tablename__ = "rules"
    __table_args__ = (
        Index(
            "ix_rules_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)  # NULL = unknown origin
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # active | orphaned | superseded | rejected (a human rejected it; kept as memory, see rule_reviews)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    # Bumped whenever statement or rationale change. A rule is validated while
    # validated_revision == revision, so an edit invalidates the validation by itself.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    validated_revision: Mapped[int | None] = mapped_column(Integer)  # NULL = never validated
    validated_by: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale_source: Mapped[str | None] = mapped_column(Text)  # conversation | human | NULL


class RuleReview(Base):
    """Append-only history of human verdicts on a rule."""

    __tablename__ = "rule_reviews"

    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # confirmed | corrected | rejected | rationale_added
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)  # GitHub login or git email
    reviewer_source: Mapped[str] = mapped_column(Text, nullable=False)  # github | cli (unverified)
    reviewer_is_session_author: Mapped[bool | None] = mapped_column(Boolean)
    statement_after: Mapped[str | None] = mapped_column(Text)
    rationale_after: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()


class RuleProposal(Base):
    """A rule a pull request would teach Rastro, shown before the merge (never in `rules`).

    Produced by running the extraction in preview mode over a PR commit that has a session.
    """

    __tablename__ = "rule_proposals"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha", "proposal_key"),
        Index("ix_rule_proposals_repository_pr", "repository_id", "pr_number"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # the original SHA, not the merge's
    proposal_key: Mapped[str] = mapped_column(Text, nullable=False)  # hash(statement + symbols)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # create | update
    existing_rule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rules.id"))
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    origin_quote: Mapped[str | None] = mapped_column(Text)
    symbols: Mapped[list[str]] = mapped_column(JSONB, nullable=False)  # path::qualified.name
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # pending | superseded (9.2); confirmed | corrected | rejected | applied come with 9.3/9.4
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class RulePreview(Base):
    """A PR commit whose preview already ran, so pushes only pay for new commits."""

    __tablename__ = "rule_previews"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    commit_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    previewed_at: Mapped[datetime] = _now()


class RuleOrigin(Base):
    __tablename__ = "rule_origins"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rules.id", ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_sessions.id"))
    commit_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    quote: Mapped[str | None] = mapped_column(Text)


class RuleAnchor(Base):
    __tablename__ = "rule_anchors"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rules.id", ondelete="CASCADE"), primary_key=True
    )
    symbol_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)  # primary | duplicate


class PrAnalysis(Base):
    """Latest impact analysis of a pull request; one row per PR, updated on every push."""

    __tablename__ = "pr_analyses"
    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number", name="uq_pr_analyses_repo_pr"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    comment_id: Mapped[int | None] = mapped_column(BigInteger)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()


class LlmCall(Base):
    """Every model call: which model, how many tokens, estimated cost (spec section 11)."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL")
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _now()
