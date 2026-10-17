"""Read access to the rules of a repository (spec section 8).

Authenticated with the repository token; the repository id in the URL must be the token's own.
`q` searches semantically when the server has an embedding model, and falls back to a plain
text match when it does not (or when the embedding call fails).
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import ColumnElement, Float, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_api.deps import CurrentRepository, DbSession
from rastro_api.schemas import (
    AnchorOut,
    OriginOut,
    RuleDetailOut,
    RuleListOut,
    RuleOut,
)
from rastro_db.models import AgentSession, Commit, Rule, RuleAnchor, RuleOrigin
from rastro_db.models import Symbol as SymbolRow
from rastro_llm import LLMError, LLMProvider

router = APIRouter(prefix="/v1/repos/{repo_id}/rules", tags=["rules"])

MIN_SEMANTIC_SIMILARITY = 0.3  # cosine similarity below this is noise, not a match
_LIKE_ESCAPE = "!"


def _own_repository(repo_id: uuid.UUID, repository_id: uuid.UUID) -> None:
    if repo_id != repository_id:
        # Same answer as for a repository that does not exist: do not reveal other tenants.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")


def _like_pattern(text: str) -> str:
    escaped = (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _anchored_in_file(path: str) -> ColumnElement[bool]:
    """The rule has an anchor in `path`, or anywhere under it when `path` is a directory."""
    wanted = path.replace("\\", "/").strip("/")
    return exists().where(
        RuleAnchor.rule_id == Rule.id,
        SymbolRow.id == RuleAnchor.symbol_id,
        or_(SymbolRow.file_path == wanted, SymbolRow.file_path.like(f"{wanted}/%")),
    )


async def _embed_query(request: Request, text: str, repository_id: uuid.UUID) -> list[float] | None:
    provider: LLMProvider | None = request.app.state.llm
    if provider is None:
        return None
    try:
        vectors = await provider.embed([text], metadata={"repository_id": str(repository_id)})
    except LLMError:
        return None
    return vectors[0] if vectors else None


@router.get("", response_model=RuleListOut)
async def list_rules(
    repo_id: uuid.UUID,
    request: Request,
    repository: CurrentRepository,
    session: DbSession,
    file: str | None = Query(default=None, description="Rules anchored in this file or directory"),
    rule_status: Literal["active", "orphaned", "superseded", "all"] = Query(
        default="active", alias="status"
    ),
    q: str | None = Query(default=None, description="Search text (semantic when available)"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RuleListOut:
    _own_repository(repo_id, repository.id)

    similarity: ColumnElement[float] | None = None
    search: Literal["semantic", "text"] | None = None
    filters: list[ColumnElement[bool]] = [Rule.repository_id == repository.id]
    if rule_status != "all":
        filters.append(Rule.status == rule_status)
    if file:
        filters.append(_anchored_in_file(file))

    order: list[Any] = [Rule.updated_at.desc()]
    if q and q.strip():
        vector = await _embed_query(request, q.strip(), repository.id)
        if vector is not None:
            search = "semantic"
            similarity = 1 - Rule.embedding.cosine_distance(vector)
            filters += [Rule.embedding.is_not(None), similarity >= MIN_SEMANTIC_SIMILARITY]
            order = [similarity.desc()]
        else:
            search = "text"
            pattern = _like_pattern(q.strip())
            filters.append(
                or_(
                    Rule.statement.ilike(pattern, escape=_LIKE_ESCAPE),
                    Rule.rationale.ilike(pattern, escape=_LIKE_ESCAPE),
                )
            )

    total = await session.scalar(select(func.count()).select_from(Rule).where(*filters)) or 0
    statement = (
        select(Rule, similarity if similarity is not None else literal(None, Float))
        .where(*filters)
        .order_by(*order)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(statement)).all()
    items = await _decorate(session, [(rule, score) for rule, score in rows])
    return RuleListOut(items=items, total=total, search=search)


@router.get("/{rule_id}", response_model=RuleDetailOut)
async def get_rule(
    repo_id: uuid.UUID,
    rule_id: uuid.UUID,
    repository: CurrentRepository,
    session: DbSession,
) -> RuleDetailOut:
    _own_repository(repo_id, repository.id)
    rule = await session.scalar(
        select(Rule).where(Rule.id == rule_id, Rule.repository_id == repository.id)
    )
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")

    [summary] = await _decorate(session, [(rule, None)])
    anchors = await session.execute(
        select(SymbolRow, RuleAnchor.role)
        .join(RuleAnchor, RuleAnchor.symbol_id == SymbolRow.id)
        .where(RuleAnchor.rule_id == rule.id)
        .order_by(
            RuleAnchor.role.desc(), SymbolRow.file_path, SymbolRow.qualified_name
        )  # primary first
    )
    return RuleDetailOut(
        **summary.model_dump(),
        origins=await _origins(session, rule),
        anchors=[
            AnchorOut(
                file_path=symbol.file_path,
                qualified_name=symbol.qualified_name,
                role=role,
                status=symbol.status,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
            )
            for symbol, role in anchors
        ],
    )


# --------------------------------------------------------------------------- helpers


async def _origins(session: AsyncSession, rule: Rule) -> list[OriginOut]:
    rows = await session.execute(
        select(RuleOrigin, Commit.committed_at, AgentSession.session_ref)
        .outerjoin(
            Commit,
            (Commit.repository_id == rule.repository_id) & (Commit.sha == RuleOrigin.commit_sha),
        )
        .outerjoin(AgentSession, AgentSession.id == RuleOrigin.session_id)
        .where(RuleOrigin.rule_id == rule.id)
        .order_by(Commit.committed_at.asc().nulls_last(), RuleOrigin.commit_sha)
    )
    return [
        OriginOut(
            commit_sha=origin.commit_sha,
            quote=origin.quote,
            committed_at=committed_at,
            session_ref=session_ref,
        )
        for origin, committed_at, session_ref in rows
    ]


async def _decorate(session: AsyncSession, rules: list[tuple[Rule, float | None]]) -> list[RuleOut]:
    """Add the files each rule is anchored in and its earliest origin."""
    if not rules:
        return []
    ids = [rule.id for rule, _ in rules]
    file_rows = await session.execute(
        select(RuleAnchor.rule_id, SymbolRow.file_path)
        .join(SymbolRow, SymbolRow.id == RuleAnchor.symbol_id)
        .where(RuleAnchor.rule_id.in_(ids))
        .distinct()
    )
    files: dict[uuid.UUID, list[str]] = {}
    for rule_id, path in file_rows:
        files.setdefault(rule_id, []).append(path)

    origin_rows = await session.execute(
        select(RuleOrigin.rule_id, RuleOrigin.commit_sha, RuleOrigin.quote, Commit.committed_at)
        .outerjoin(
            Commit,
            (Commit.repository_id == rules[0][0].repository_id)
            & (Commit.sha == RuleOrigin.commit_sha),
        )
        .where(RuleOrigin.rule_id.in_(ids))
        .order_by(Commit.committed_at.asc().nulls_last(), RuleOrigin.commit_sha)
    )
    first_origin: dict[uuid.UUID, OriginOut] = {}
    for rule_id, sha, quote, committed_at in origin_rows:
        first_origin.setdefault(
            rule_id,
            OriginOut(commit_sha=sha, quote=quote, committed_at=committed_at, session_ref=None),
        )

    return [
        RuleOut(
            id=rule.id,
            statement=rule.statement,
            rationale=rule.rationale,
            confidence=rule.confidence,
            status=rule.status,
            created_at=_aware(rule.created_at),
            updated_at=_aware(rule.updated_at),
            files=sorted(files.get(rule.id, [])),
            origin=first_origin.get(rule.id),
            similarity=score,
        )
        for rule, score in rules
    ]


def _aware(value: datetime) -> datetime:
    return value
