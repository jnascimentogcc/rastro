"""Read access to the rules of a repository (spec section 8).

Authenticated with the repository token; the repository id in the URL must be the token's own.
`q` searches semantically when the server has an embedding model, and falls back to a plain
text match when it does not (or when the embedding call fails).
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import ColumnElement, Float, and_, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_api.deps import CurrentRepository, DbSession
from rastro_api.schemas import (
    AnchorOut,
    OriginOut,
    ReviewIn,
    ReviewOut,
    RuleDetailOut,
    RuleListOut,
    RuleOut,
    RulesSnapshotOut,
    SnapshotAnchorOut,
    SnapshotRuleOut,
    Validation,
)
from rastro_db.budget import budget_exhausted
from rastro_db.models import AgentSession, Commit, Rule, RuleAnchor, RuleOrigin, RuleReview
from rastro_db.models import Symbol as SymbolRow
from rastro_llm import LLMError, LLMProvider

router = APIRouter(prefix="/v1/repos/{repo_id}/rules", tags=["rules"])

MIN_SEMANTIC_SIMILARITY = 0.3  # cosine similarity below this is noise, not a match
_LIKE_ESCAPE = "!"
SNAPSHOT_MAX_RULES = 5_000  # beyond this the snapshot says `truncated` instead of growing


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


async def _embed_query(
    request: Request, session: AsyncSession, text: str, repository_id: uuid.UUID
) -> list[float] | None:
    provider: LLMProvider | None = request.app.state.llm
    if provider is None:
        return None
    if await budget_exhausted(session, repository_id, request.app.state.llm_monthly_token_budget):
        return None  # over the monthly budget: plain text search costs nothing
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
    rule_status: Literal["active", "orphaned", "superseded", "rejected", "all"] = Query(
        default="active", alias="status"
    ),
    validated: bool | None = Query(
        default=None, description="true: confirmed by a human; false: not yet"
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
    if validated is True:
        filters.append(and_(Rule.status != "rejected", Rule.validated_revision == Rule.revision))
    elif validated is False:
        filters.append(Rule.status != "rejected")
        filters.append(
            or_(Rule.validated_revision.is_(None), Rule.validated_revision != Rule.revision)
        )

    order: list[Any] = [Rule.updated_at.desc()]
    if q and q.strip():
        vector = await _embed_query(request, session, q.strip(), repository.id)
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


@router.get("/snapshot", response_model=RulesSnapshotOut)
async def rules_snapshot(
    repo_id: uuid.UUID,
    request: Request,
    repository: CurrentRepository,
    session: DbSession,
) -> Response:
    """Every active rule with its active anchors, for the local cache read by the agent hook.

    The ETag hashes the content served (not row counts or timestamps): anchors move on resync and
    reviews change validation without always touching `rules.updated_at`, and a stale cache would
    fail silently. `If-None-Match` answers 304.
    """
    _own_repository(repo_id, repository.id)
    rows = (
        await session.scalars(
            select(Rule)
            .where(Rule.repository_id == repository.id, Rule.status == "active")
            .order_by(Rule.updated_at.desc(), Rule.id)
            .limit(SNAPSHOT_MAX_RULES + 1)
        )
    ).all()
    truncated = len(rows) > SNAPSHOT_MAX_RULES
    rules = list(rows[:SNAPSHOT_MAX_RULES])

    anchors: dict[uuid.UUID, list[SnapshotAnchorOut]] = {}
    anchor_rows = await session.execute(
        select(RuleAnchor.rule_id, SymbolRow.file_path, SymbolRow.qualified_name, RuleAnchor.role)
        .join(SymbolRow, SymbolRow.id == RuleAnchor.symbol_id)
        .where(RuleAnchor.rule_id.in_([rule.id for rule in rules]), SymbolRow.status == "active")
        .order_by(RuleAnchor.role.desc(), SymbolRow.file_path, SymbolRow.qualified_name)
    )  # primary first
    for rule_id, file_path, qualified_name, role in anchor_rows:
        anchors.setdefault(rule_id, []).append(
            SnapshotAnchorOut(file_path=file_path, qualified_name=qualified_name, role=role)
        )

    head_sha = await session.scalar(
        select(Commit.sha)
        .where(Commit.repository_id == repository.id, Commit.on_default_branch.is_(True))
        .order_by(Commit.committed_at.desc().nulls_last())
        .limit(1)
    )
    items = [
        SnapshotRuleOut(
            id=rule.id,
            statement=rule.statement,
            rationale=rule.rationale,
            rationale_source=rule.rationale_source,
            validation=_validation(rule),
            revision=rule.revision,
            validated_by=rule.validated_by if _validation(rule) == "validated" else None,
            validated_at=rule.validated_at if _validation(rule) == "validated" else None,
            anchors=anchors[rule.id],
        )
        for rule in rules
        if rule.id in anchors  # a rule with no active anchor cannot advise on any file
    ]
    body = RulesSnapshotOut(
        head_sha=head_sha, generated_at=datetime.now(UTC), truncated=truncated, rules=items
    )
    content = body.model_dump(mode="json")
    stable = {key: value for key, value in content.items() if key != "generated_at"}
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:32]
    etag = f'"{digest}"'
    if etag in {tag.strip() for tag in request.headers.get("if-none-match", "").split(",")}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return JSONResponse(content, headers={"ETag": etag})


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
    return await _detail(session, rule)


@router.post("/{rule_id}/reviews", response_model=RuleDetailOut)
async def review_rule(
    repo_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: ReviewIn,
    request: Request,
    repository: CurrentRepository,
    session: DbSession,
) -> RuleDetailOut:
    """A human confirms, corrects, rejects or explains a rule.

    The reviewer name comes from the client (the CLI sends the git email), so it is recorded as
    `reviewer_source = "cli"`: anyone holding the repository token could write another name.
    """
    _own_repository(repo_id, repository.id)
    rule = await session.scalar(
        select(Rule)
        .where(Rule.id == rule_id, Rule.repository_id == repository.id)
        .with_for_update()
    )
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    if rule.status == "rejected" and body.verdict != "rejected":
        raise HTTPException(status.HTTP_409_CONFLICT, "Rule was rejected")
    if rule.status == "superseded":
        raise HTTPException(status.HTTP_409_CONFLICT, "Rule was superseded")

    now = datetime.now(UTC)
    if body.verdict == "rejected":
        rule.status = "rejected"
    elif body.verdict == "confirmed":
        _validate_current_revision(rule, body.reviewer, now)
    else:
        statement_changes = body.verdict == "corrected" and body.statement != rule.statement
        if body.rationale:
            rule.rationale, rule.rationale_source = body.rationale, "human"
        if statement_changes and body.statement:
            rule.statement = body.statement
            vector = await _embed_query(request, session, body.statement, repository.id)
            if vector is not None:  # without a model the old embedding stays (slightly stale)
                rule.embedding = vector
        if statement_changes or body.rationale:
            rule.revision += 1
        if body.verdict == "corrected":  # whoever corrects a rule stands behind the new text
            _validate_current_revision(rule, body.reviewer, now)
    rule.updated_at = now
    session.add(
        RuleReview(
            rule_id=rule.id,
            rule_revision=rule.revision,
            verdict=body.verdict,
            reviewer=body.reviewer,
            reviewer_source="cli",
            statement_after=rule.statement if body.verdict == "corrected" else None,
            rationale_after=body.rationale,
            note=body.note,
        )
    )
    await session.commit()
    await session.refresh(rule)
    return await _detail(session, rule)


def _validate_current_revision(rule: Rule, reviewer: str, when: datetime) -> None:
    rule.validated_revision, rule.validated_by, rule.validated_at = rule.revision, reviewer, when


# --------------------------------------------------------------------------- helpers


async def _detail(session: AsyncSession, rule: Rule) -> RuleDetailOut:
    [summary] = await _decorate(session, [(rule, None)])
    anchors = await session.execute(
        select(SymbolRow, RuleAnchor.role)
        .join(RuleAnchor, RuleAnchor.symbol_id == SymbolRow.id)
        .where(RuleAnchor.rule_id == rule.id)
        .order_by(
            RuleAnchor.role.desc(), SymbolRow.file_path, SymbolRow.qualified_name
        )  # primary first
    )
    reviews = await session.scalars(
        select(RuleReview)
        .where(RuleReview.rule_id == rule.id)
        .order_by(RuleReview.created_at.desc(), RuleReview.id)
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
        reviews=[
            ReviewOut(
                verdict=r.verdict,
                rule_revision=r.rule_revision,
                reviewer=r.reviewer,
                reviewer_source=r.reviewer_source,
                statement_after=r.statement_after,
                rationale_after=r.rationale_after,
                note=r.note,
                created_at=r.created_at,
            )
            for r in reviews
        ],
    )


def _validation(rule: Rule) -> Validation:
    if rule.status == "rejected":
        return "rejected"
    return "validated" if rule.validated_revision == rule.revision else "unvalidated"


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
            rationale_source=rule.rationale_source,
            confidence=rule.confidence,
            status=rule.status,
            validation=_validation(rule),
            revision=rule.revision,
            validated_by=rule.validated_by if _validation(rule) == "validated" else None,
            validated_at=rule.validated_at if _validation(rule) == "validated" else None,
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
