"""Turn a commit plus the captured agent session into business rules (spec flow B, steps 2-5).

The model proposes; this module disposes. Everything the model returns is validated against
what was actually in the prompt (symbol keys, existing rule ids, quotes) so a hallucinated
anchor or a made-up reason never reaches the database.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_code_analysis import (
    FileAnalysis,
    Symbol,
    analyze_source,
    symbol_source,
    touched_symbols,
)
from rastro_db.models import (
    AgentSession,
    Commit,
    Repository,
    Rule,
    RuleAnchor,
    RuleOrigin,
    SymbolCall,
)
from rastro_db.models import Symbol as SymbolRow
from rastro_github import ChangedFile
from rastro_llm import (
    ExtractedRule,
    ExtractionContext,
    LLMError,
    LLMProvider,
    LLMQuotaError,
    LLMTransientError,
    PromptExcerpt,
    PromptFileDiff,
    PromptRule,
    PromptSymbol,
    RuleExtraction,
    build_extraction_prompt,
)
from rastro_worker.deps import WorkerDeps
from rastro_worker.indexing import ensure_symbols, is_indexable

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.5
DUPLICATE_SIMILARITY = 0.92
DUPLICATE_MIN_LINES = 3
NEARBY_RULES_LIMIT = 6  # rules of neighbouring code offered to the model besides the direct ones
NEARBY_UPDATE_MIN_SIMILARITY = 0.6  # an "update" of a nearby rule must still resemble it
SYMBOL_EMBEDDING_CHARS = 2_000
ATTEMPTS = 2
_NOISE_FILES = re.compile(r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|\.min\.(js|css)|\.map)$")


@dataclass
class ExtractionSummary:
    # analyzed | analysis_failed | skipped_no_session | skipped_no_llm | skipped_budget | skipped_quota | awaiting_merge
    status: str
    created: int = 0
    updated: int = 0
    discarded: int = 0
    unchanged: int = 0  # updates that would change nothing (typically refactors)
    duplicates_linked: int = 0
    rule_ids: list[uuid.UUID] = field(default_factory=list)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


_WORD = re.compile(r"[a-z]+|[0-9]+")  # "48h" -> "48", "h"
_FILLER = frozenset(
    [
        "that",
        "this",
        "with",
        "because",
        "they",
        "their",
        "have",
        "from",
        "will",
        "would",
        "should",
        "could",
        "when",
        "which",
        "there",
        "been",
        "also",
        "only",
        "than",
        "then",
        "them",
        "what",
        "were",
        "into",
        "more",
        "some",
        "such",
        "does",
        "done",
        "make",
        "made",
        "need",
        "needs",
    ]
)
GROUNDING_THRESHOLD = 0.5


def _content_stems(text: str) -> set[str]:
    """Distinctive words of a text (numbers and words of 4+ letters, minus filler), cut to five
    letters so that "follow-up"/"following" or "insurer"/"insurers" still match."""
    return {
        word[:5]
        for word in _WORD.findall(text.lower())
        if (word.isdigit() or len(word) >= 4) and word not in _FILLER
    }


def _grounded(rationale: str, request_texts: list[str]) -> bool:
    words = _content_stems(rationale)
    if not words:
        return False
    known = (
        set().union(*(_content_stems(text) for text in request_texts)) if request_texts else set()
    )
    return len(words & known) / len(words) >= GROUNDING_THRESHOLD


def _key(path: str, qualified_name: str) -> str:
    return f"{path}::{qualified_name}"


# --------------------------------------------------------------------------- entry point


async def extract_rules(
    deps: WorkerDeps, repository_id: uuid.UUID, sha: str, *, force: bool = False
) -> ExtractionSummary:
    llm = deps.llm
    if llm is None:
        logger.warning("rule extraction skipped for %s: no LLM configured", sha)
        return ExtractionSummary("skipped_no_llm")

    async with deps.session_factory() as session:
        repository = await session.get(Repository, repository_id)
        if repository is None or repository.installation_id == 0:
            return ExtractionSummary("skipped_no_llm")
        commit = await session.scalar(
            select(Commit).where(Commit.repository_id == repository_id, Commit.sha == sha)
        )
        sessions = list(
            await session.scalars(
                select(AgentSession)
                .where(AgentSession.repository_id == repository_id, AgentSession.commit_sha == sha)
                .order_by(AgentSession.captured_at)
            )
        )
    if not sessions:
        # MVP decision (spec section 14): commits without a captured session are not analysed.
        await _set_status(deps, repository_id, sha, "skipped_no_session")
        return ExtractionSummary("skipped_no_session")
    if not force and commit is not None and commit.on_default_branch is False:
        # Rules describe the default branch. Extracting from a feature-branch commit would rewrite
        # them before the pull request is analysed (hiding its own warning) and would leave them
        # wrong if the PR is never merged. The push that merges the commit queues this again.
        await _set_status(deps, repository_id, sha, "awaiting_merge")
        return ExtractionSummary("awaiting_merge")
    if (
        not force
        and commit is not None
        and commit.analysis_status == "analyzed"
        and commit.analyzed_at is not None
        and all(s.captured_at <= commit.analyzed_at for s in sessions)
    ):
        return ExtractionSummary("analyzed")

    if await deps.over_budget(repository_id):
        # Visible on the commit; re-run `rule_extraction` with force=True once budget exists.
        await _set_status(deps, repository_id, sha, "skipped_budget")
        return ExtractionSummary("skipped_budget")

    changes = await deps.github.get_commit_files(
        repository.installation_id, repository.full_name, sha
    )
    analyses, touched, sources = await _analyze_changes(deps, repository, changes)
    if not touched:
        await _set_status(deps, repository_id, sha, "analyzed")
        return ExtractionSummary("analyzed")

    async with deps.session_factory() as session, session.begin():
        ids = await ensure_symbols(session, repository_id, sha, analyses)
        existing, nearby = await _existing_rules(session, repository_id, ids, touched)

    context = ExtractionContext(
        commit_message=(commit.message if commit and commit.message else ""),
        excerpts=_prompt_excerpts(sessions),
        diffs=[
            PromptFileDiff(c.path, c.patch)
            for c in changes
            if c.patch and c.status != "removed" and not _NOISE_FILES.search(c.path)
        ],
        symbols=[
            PromptSymbol(_key(path, s.qualified_name), s.kind, symbol_source(sources[path], s))
            for path, symbols in touched.items()
            for s in symbols
        ],
        existing_rules=existing,
    )
    try:
        extraction = await _ask_model(
            llm, build_extraction_prompt(context), {"repository_id": str(repository_id), "sha": sha}
        )
        if extraction is None:
            await _set_status(deps, repository_id, sha, "analysis_failed")
            return ExtractionSummary("analysis_failed")

        # Embeddings are requested before anything is written, so a quota error here leaves the
        # database untouched.
        summary = await _apply(
            deps,
            llm,
            repository_id,
            sha,
            sessions,
            extraction,
            ids,
            touched,
            sources,
            existing,
            nearby,
        )
    except LLMQuotaError as error:
        logger.error(
            "rule extraction for commit %s skipped: the LLM account has no credit left (%s). "
            "Top it up, then run `python -m rastro_worker.requeue` to analyse it.",
            sha,
            error,
        )
        await _set_status(deps, repository_id, sha, "skipped_quota")
        return ExtractionSummary("skipped_quota")
    await _set_status(deps, repository_id, sha, "analyzed")
    return summary


# --------------------------------------------------------------------------- helpers


async def _set_status(deps: WorkerDeps, repository_id: uuid.UUID, sha: str, status: str) -> None:
    async with deps.session_factory() as session, session.begin():
        await session.execute(
            insert(Commit)
            .values(repository_id=repository_id, sha=sha)
            .on_conflict_do_nothing(index_elements=["repository_id", "sha"])
        )
        await session.execute(
            update(Commit)
            .where(Commit.repository_id == repository_id, Commit.sha == sha)
            .values(
                analysis_status=status,
                analyzed_at=datetime.now(UTC) if status == "analyzed" else None,
            )
        )


async def _analyze_changes(
    deps: WorkerDeps, repository: Repository, changes: list[ChangedFile]
) -> tuple[list[FileAnalysis], dict[str, list[Symbol]], dict[str, str]]:
    """Analyse the changed source files; return the analyses, the symbols each patch touches,
    and the file contents (kept in memory only, to cut symbol source for the prompt)."""
    analyses: list[FileAnalysis] = []
    touched: dict[str, list[Symbol]] = {}
    sources: dict[str, str] = {}
    for change in changes:
        if change.status == "removed" or not change.blob_sha or not is_indexable(change.path, 0):
            continue
        blob = await deps.github.get_blob(
            repository.installation_id, repository.full_name, change.blob_sha
        )
        text = blob.decode("utf-8", errors="replace")
        analysis = analyze_source(text, change.path)
        analyses.append(analysis)
        sources[change.path] = text
        hit = touched_symbols(list(analysis.symbols), change.patch, analysis.declarations)
        if hit:
            touched[change.path] = hit
    return analyses, touched, sources


async def _existing_rules(
    session: AsyncSession,
    repository_id: uuid.UUID,
    ids: dict[tuple[str, str], uuid.UUID],
    touched: dict[str, list[Symbol]],
) -> tuple[list[PromptRule], set[str]]:
    """Active rules the change may be touching: anchored to a changed symbol, or anchored to a
    symbol with identical normalized code elsewhere (the copy-paste case: the agent duplicates
    existing logic, so the model should update that rule instead of inventing a new one).

    A few rules of NEARBY code follow (same file, or a direct caller/callee of a changed symbol).
    Without them, a rule that a later commit restates in code it did not anchor to (the handler
    that calls the validator, the constant that feeds it) is invisible to the model, which then
    creates a second phrasing of the same rule. Their ids are returned separately because an
    "update" of one of them gets an extra check in `_apply`."""
    touched_ids = {
        ids[(path, s.qualified_name)]: _key(path, s.qualified_name)
        for path, symbols in touched.items()
        for s in symbols
        if (path, s.qualified_name) in ids
    }
    if not touched_ids:
        return [], set()
    hashes = {s.body_hash for symbols in touched.values() for s in symbols if s.kind != "class"}
    anchored = SymbolRow.id.in_(touched_ids)
    same_code = and_(
        SymbolRow.body_hash.in_(hashes),
        SymbolRow.kind != "class",
        SymbolRow.status == "active",
        SymbolRow.end_line - SymbolRow.start_line + 1 >= DUPLICATE_MIN_LINES,
    )
    rows = await session.execute(
        select(Rule, SymbolRow.id, SymbolRow.file_path, SymbolRow.qualified_name)
        .join(RuleAnchor, RuleAnchor.rule_id == Rule.id)
        .join(SymbolRow, SymbolRow.id == RuleAnchor.symbol_id)
        .where(
            Rule.repository_id == repository_id,
            Rule.status == "active",
            SymbolRow.repository_id == repository_id,
            or_(anchored, same_code),
        )
    )
    grouped: dict[uuid.UUID, tuple[Rule, list[str]]] = {}
    for rule, symbol_id, path, name in rows:
        keys = grouped.setdefault(rule.id, (rule, []))[1]
        keys.append(touched_ids.get(symbol_id) or _key(path, name))
    nearby = await _nearby_rules(session, repository_id, set(touched_ids), set(touched), grouped)
    rules = [
        PromptRule(str(rule.id), rule.statement, rule.rationale, tuple(keys))
        for rule, keys in [*grouped.values(), *nearby.values()]
    ]
    return rules, {str(rule_id) for rule_id in nearby}


async def _nearby_rules(
    session: AsyncSession,
    repository_id: uuid.UUID,
    touched_ids: set[uuid.UUID],
    touched_files: set[str],
    direct: dict[uuid.UUID, tuple[Rule, list[str]]],
) -> dict[uuid.UUID, tuple[Rule, list[str]]]:
    callers = select(SymbolCall.caller_id).where(SymbolCall.callee_id.in_(touched_ids))
    callees = select(SymbolCall.callee_id).where(SymbolCall.caller_id.in_(touched_ids))
    rows = await session.execute(
        select(Rule, SymbolRow.file_path, SymbolRow.qualified_name)
        .join(RuleAnchor, RuleAnchor.rule_id == Rule.id)
        .join(SymbolRow, SymbolRow.id == RuleAnchor.symbol_id)
        .where(
            Rule.repository_id == repository_id,
            Rule.status == "active",
            Rule.id.not_in(list(direct)) if direct else true(),
            SymbolRow.repository_id == repository_id,
            SymbolRow.status == "active",
            or_(
                SymbolRow.file_path.in_(touched_files),
                SymbolRow.id.in_(callers),
                SymbolRow.id.in_(callees),
            ),
        )
        .order_by(Rule.updated_at.desc())
    )
    grouped: dict[uuid.UUID, tuple[Rule, list[str]]] = {}
    for rule, path, name in rows:
        if rule.id not in grouped and len(grouped) >= NEARBY_RULES_LIMIT:
            continue
        grouped.setdefault(rule.id, (rule, []))[1].append(_key(path, name))
    return grouped


def _prompt_excerpts(sessions: list[AgentSession]) -> list[PromptExcerpt]:
    order = {"intent": 0, "context": 1, "assistant_note": 2}
    excerpts = [
        PromptExcerpt(str(e.get("kind")), str(e.get("text", "")))
        for s in sessions
        for e in s.excerpts
        if e.get("kind") in order
    ]
    return sorted(excerpts, key=lambda e: order[e.kind])


async def _ask_model(
    llm: LLMProvider, prompt: str, metadata: dict[str, Any]
) -> RuleExtraction | None:
    """One retry on refusal or invalid output; a second failure marks the commit as failed
    instead of raising, so a bad answer never blocks the queue."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return await llm.extract(prompt, RuleExtraction, metadata=metadata)
        except (LLMQuotaError, LLMTransientError):
            raise  # quota: skipped by the caller; transient: the job retries with backoff
        except (LLMError, ValidationError) as error:
            logger.warning("rule extraction attempt %d/%d failed: %s", attempt, ATTEMPTS, error)
    return None


# --------------------------------------------------------------------------- applying


@dataclass
class _Accepted:
    rule: ExtractedRule
    symbols: list[str]
    rationale: str | None
    quote: str | None
    session_id: uuid.UUID | None
    existing_id: uuid.UUID | None


def _validate(
    extraction: RuleExtraction,
    sessions: list[AgentSession],
    valid_keys: set[str],
    existing: list[PromptRule],
) -> tuple[list[_Accepted], int, int]:
    existing_by_id = {r.id: r for r in existing}
    user_texts = [
        (s.id, _normalize(str(e.get("text", ""))))
        for s in sessions
        for e in s.excerpts
        if e.get("role") == "user" and e.get("kind") in ("intent", "context")
    ]
    accepted: list[_Accepted] = []
    discarded = 0
    unchanged = 0
    for rule in extraction.rules:
        symbols = [k for k in dict.fromkeys(rule.symbols) if k in valid_keys]
        if (
            rule.action == "none"
            or rule.confidence < MIN_CONFIDENCE
            or not rule.statement.strip()
            or not symbols
        ):
            discarded += 1
            continue

        quote = rule.origin_quote.strip() if rule.origin_quote else None
        session_id: uuid.UUID | None = None
        if quote:
            needle = _normalize(quote)
            session_id = next((sid for sid, text in user_texts if needle in text), None)
            if session_id is None:
                quote = None  # not verbatim: do not store a made-up citation

        # Never invent the why: keep a rationale only if what it says is grounded in the user's
        # own words (the model may paraphrase, but it cannot bring in new facts).
        rationale = rule.rationale.strip() if rule.rationale else None
        if rationale and not _grounded(rationale, [text for _, text in user_texts]):
            logger.warning("rationale dropped, not grounded in the requests: %r", rationale)
            rationale = None

        existing_id = None
        if rule.action == "update" and rule.existing_rule_id in existing_by_id:
            existing_id = uuid.UUID(str(rule.existing_rule_id))
            current = existing_by_id[rule.existing_rule_id]
            restated = rationale is not None and _normalize(rationale) == _normalize(
                current.rationale or ""
            )
            if (
                _normalize(rule.statement) == _normalize(current.statement)
                and (not rationale or restated)
                and set(symbols) <= set(current.symbol_keys)
            ):
                unchanged += 1  # the model "updated" a rule without changing anything
                continue
        accepted.append(
            _Accepted(
                rule, symbols, rationale or None, quote, session_id or sessions[0].id, existing_id
            )
        )
    return accepted, discarded, unchanged


async def _apply(
    deps: WorkerDeps,
    llm: LLMProvider,
    repository_id: uuid.UUID,
    sha: str,
    sessions: list[AgentSession],
    extraction: RuleExtraction,
    ids: dict[tuple[str, str], uuid.UUID],
    touched: dict[str, list[Symbol]],
    sources: dict[str, str],
    existing: list[PromptRule],
    nearby: set[str] | None = None,
) -> ExtractionSummary:
    key_to_symbol: dict[str, tuple[str, Symbol]] = {
        _key(path, s.qualified_name): (path, s)
        for path, symbols in touched.items()
        for s in symbols
    }
    accepted, discarded, unchanged = _validate(extraction, sessions, set(key_to_symbol), existing)
    summary = ExtractionSummary("analyzed", discarded=discarded, unchanged=unchanged)
    if not accepted:
        return summary

    metadata = {"repository_id": str(repository_id), "sha": sha}
    statement_vectors = await llm.embed([a.rule.statement for a in accepted], metadata=metadata)
    await _reject_unrelated_updates(deps, accepted, statement_vectors, nearby or set())
    anchored_keys = sorted({k for a in accepted for k in a.symbols})
    symbol_vectors = dict(
        zip(
            anchored_keys,
            await llm.embed(
                [
                    symbol_source(sources[key_to_symbol[k][0]], key_to_symbol[k][1])[
                        :SYMBOL_EMBEDDING_CHARS
                    ]
                    for k in anchored_keys
                ],
                metadata=metadata,
            ),
            strict=True,
        )
    )

    async with deps.session_factory() as session, session.begin():
        for key, vector in symbol_vectors.items():
            path, symbol = key_to_symbol[key]
            await session.execute(
                update(SymbolRow)
                .where(SymbolRow.id == ids[(path, symbol.qualified_name)])
                .values(embedding=vector)
            )
        for item, vector in zip(accepted, statement_vectors, strict=True):
            rule_id = await _store_rule(session, repository_id, sha, item, vector)
            summary.rule_ids.append(rule_id)
            if item.existing_id is None:
                summary.created += 1
            else:
                summary.updated += 1
            for key in item.symbols:
                path, symbol = key_to_symbol[key]
                await session.execute(
                    insert(RuleAnchor)
                    .values(
                        rule_id=rule_id,
                        symbol_id=ids[(path, symbol.qualified_name)],
                        role="primary",
                    )
                    .on_conflict_do_update(
                        index_elements=["rule_id", "symbol_id"], set_={"role": "primary"}
                    )
                )
            summary.duplicates_linked += await _link_duplicates(
                session,
                repository_id,
                rule_id,
                [
                    (
                        ids[key_to_symbol[k][0], key_to_symbol[k][1].qualified_name],
                        key_to_symbol[k][1].body_hash,
                        symbol_vectors[k],
                    )
                    for k in item.symbols
                ],
            )
    return summary


async def _reject_unrelated_updates(
    deps: WorkerDeps,
    accepted: list[_Accepted],
    vectors: list[list[float]],
    nearby: set[str],
) -> None:
    """The model may update a rule of nearby code only if the new statement still resembles it.

    Direct rules are anchored to what the commit changed, so an update there is expected to be
    free-form. A nearby rule is only offered so that a restated rule is not duplicated; if the
    "update" is about something else entirely, it is really a new rule, and overwriting the nearby
    one would destroy it.
    """
    pending = [
        (item, vector)
        for item, vector in zip(accepted, vectors, strict=True)
        if item.existing_id is not None and str(item.existing_id) in nearby
    ]
    if not pending:
        return
    async with deps.session_factory() as session:
        for item, vector in pending:
            distance = await session.scalar(
                select(Rule.embedding.cosine_distance(vector)).where(Rule.id == item.existing_id)
            )
            if distance is not None and 1 - float(distance) < NEARBY_UPDATE_MIN_SIMILARITY:
                logger.info(
                    "update of nearby rule %s is unrelated to it (similarity %.2f): stored as new",
                    item.existing_id,
                    1 - float(distance),
                )
                item.existing_id = None


async def _store_rule(
    session: AsyncSession,
    repository_id: uuid.UUID,
    sha: str,
    item: _Accepted,
    vector: list[float],
) -> uuid.UUID:
    rule = item.rule
    if item.existing_id is None:
        rule_id = (
            await session.execute(
                insert(Rule)
                .values(
                    repository_id=repository_id,
                    statement=rule.statement.strip(),
                    rationale=item.rationale,
                    confidence=rule.confidence,
                    embedding=vector,
                )
                .returning(Rule.id)
            )
        ).scalar_one()
    else:
        rule_id = item.existing_id
        values: dict[str, Any] = {
            "statement": rule.statement.strip(),
            "confidence": rule.confidence,
            "embedding": vector,
            "updated_at": func.now(),
        }
        if item.rationale:  # a new commit without a stated reason must not erase the old one
            values["rationale"] = item.rationale
        await session.execute(update(Rule).where(Rule.id == rule_id).values(**values))
    await session.execute(
        insert(RuleOrigin)
        .values(rule_id=rule_id, session_id=item.session_id, commit_sha=sha, quote=item.quote)
        .on_conflict_do_update(
            index_elements=["rule_id", "commit_sha"],
            set_={"session_id": item.session_id, "quote": item.quote},
        )
    )
    return rule_id


async def _link_duplicates(
    session: AsyncSession,
    repository_id: uuid.UUID,
    rule_id: uuid.UUID,
    primaries: list[tuple[uuid.UUID, str, list[float]]],
) -> int:
    """Anchor other symbols that implement the same logic as `duplicate`: identical normalized
    AST hash, or embedding similarity above the threshold (only symbols that have an
    embedding take part in the latter; full indexing deliberately computes none)."""
    linked: set[uuid.UUID] = set()
    primary_ids = {pid for pid, _, _ in primaries}
    # `digest` comes from the analysed commit, not from the row, which may hold the default
    # branch version of the symbol.
    for symbol_id, digest, vector in primaries:
        base = [
            SymbolRow.repository_id == repository_id,
            SymbolRow.status == "active",
            SymbolRow.kind != "class",
            SymbolRow.id != symbol_id,
            SymbolRow.end_line - SymbolRow.start_line + 1 >= DUPLICATE_MIN_LINES,
        ]
        exact = await session.scalars(
            select(SymbolRow.id).where(*base, SymbolRow.body_hash == digest)
        )
        similar = await session.scalars(
            select(SymbolRow.id).where(
                *base,
                SymbolRow.embedding.is_not(None),
                SymbolRow.embedding.cosine_distance(vector) <= 1 - DUPLICATE_SIMILARITY,
            )
        )
        linked.update(set(exact) | set(similar))
    linked -= primary_ids
    for duplicate_id in linked:
        await session.execute(
            insert(RuleAnchor)
            .values(rule_id=rule_id, symbol_id=duplicate_id, role="duplicate")
            .on_conflict_do_nothing(index_elements=["rule_id", "symbol_id"])
        )
    return len(linked)
