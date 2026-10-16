"""SQLAlchemy models. Reference DDL: docs/spec-rastro-mvp.md section 6."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
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
    created_at: Mapped[datetime] = _now()


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
    # analyzed | analysis_failed | skipped_no_session | NULL (not analyzed yet)
    analysis_status: Mapped[str | None] = mapped_column(Text)


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
    excerpts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
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
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


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
    __tablename__ = "pr_analyses"

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
