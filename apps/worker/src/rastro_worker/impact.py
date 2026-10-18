"""Pull request impact analysis (spec flow C, section 7.7).

A pull request is mapped onto the symbols it changes; rules anchored to those symbols (or close to
them in the call graph, or whose copies were left unchanged) become candidates; the model decides
for each one whether the change can violate or change it; only confident findings are posted, in
one comment that is edited in place on every push.
"""

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select
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
    Commit,
    PrAnalysis,
    Repository,
    Rule,
    RuleAnchor,
    RuleOrigin,
    SymbolCall,
)
from rastro_db.models import Symbol as SymbolRow
from rastro_github import ChangedFile, CommentNotFoundError
from rastro_llm import (
    ImpactAssessment,
    ImpactContext,
    LLMError,
    LLMProvider,
    LLMQuotaError,
    LLMTransientError,
    PromptFileDiff,
    PromptSymbol,
    build_impact_prompt,
)
from rastro_worker.comment import Finding, render_all_clear, render_comment
from rastro_worker.deps import WorkerDeps
from rastro_worker.indexing import is_indexable

logger = logging.getLogger(__name__)

RELATED_DEPTH = 2
MAX_RELATED_CHANGED_SYMBOLS = 6
ATTEMPTS = 2


@dataclass
class ImpactResult:
    # analyzed | skipped_no_llm | skipped_unknown_repository | skipped_budget | skipped_quota
    status: str
    candidates: int = 0
    assessed: int = 0
    reported: int = 0
    commented: bool = False
    comment_id: int | None = None


@dataclass
class _Candidate:
    rule: Rule
    relation: str  # direct | related
    touched_keys: list[str] = field(default_factory=list)  # the rule's symbols the PR touches
    unchanged_duplicates: list[str] = field(
        default_factory=list
    )  # "path::name" of untouched copies
    merged_rule_ids: list[str] = field(default_factory=list)  # near-identical rules folded in


# Cosine similarity of two rule statements above which they are treated as one rule.
RULE_MERGE_SIMILARITY = 0.9


def _key(path: str, name: str) -> str:
    return f"{path}::{name}"


# --------------------------------------------------------------------------- entry point


async def analyze_pull_request(
    deps: WorkerDeps, repository_id: uuid.UUID, number: int, head_sha: str
) -> ImpactResult:
    llm = deps.llm
    if llm is None:
        logger.warning("impact analysis skipped for PR #%d: no LLM configured", number)
        return ImpactResult("skipped_no_llm")
    async with deps.session_factory() as session:
        repository = await session.get(Repository, repository_id)
    if repository is None or repository.installation_id == 0:
        return ImpactResult("skipped_unknown_repository")
    if await deps.over_budget(repository_id):
        return ImpactResult("skipped_budget")  # no model call, and no comment either

    files = await deps.github.get_pull_request_files(
        repository.installation_id, repository.full_name, number
    )
    head = await _analyze_head(deps, repository, files)

    async with deps.session_factory() as session:
        direct_ids, key_by_id = await _changed_symbol_ids(session, repository_id, files, head)
        candidates = await _candidate_rules(
            session, repository_id, direct_ids, key_by_id, deps.impact.impact_max_candidates
        )

    result = ImpactResult("analyzed", candidates=len(candidates))
    try:
        assessments = await _assess(deps, llm, repository_id, number, candidates, files, head)
    except LLMQuotaError as error:
        # No comment and no record: "nothing affected" must not be confused with "not analysed".
        logger.error(
            "impact analysis of PR #%d skipped: the LLM account has no credit left (%s). "
            "Push to the PR again once it is topped up.",
            number,
            error,
        )
        result.status = "skipped_quota"
        return result
    result.assessed = len(assessments)

    threshold = deps.impact.impact_threshold
    reported = sorted(
        (a for a in assessments if _reportable(a, threshold)),
        key=lambda a: a[2].confidence,
        reverse=True,
    )
    result.reported = len(reported)
    await _publish(deps, repository, number, head_sha, assessments, reported, result)
    return result


def _reportable(entry: tuple[_Candidate, str, ImpactAssessment], threshold: float) -> bool:
    candidate, _, assessment = entry
    risk = _effective_risk(candidate, assessment)
    return risk != "none" and assessment.confidence >= threshold


def _effective_risk(candidate: _Candidate, assessment: ImpactAssessment) -> str:
    """The model may not claim an unchanged copy exists when there is none."""
    if assessment.risk == "inconsistent_duplicate" and not candidate.unchanged_duplicates:
        return "changes"
    return assessment.risk


# --------------------------------------------------------------------------- the change


@dataclass
class _Head:
    analyses: dict[str, FileAnalysis]
    sources: dict[str, str]
    touched: dict[str, list[Symbol]]
    hashes: set[str]


async def _analyze_head(
    deps: WorkerDeps, repository: Repository, files: list[ChangedFile]
) -> _Head:
    head = _Head({}, {}, {}, set())
    for change in files:
        if change.status == "removed" or not change.blob_sha or not is_indexable(change.path, 0):
            continue
        blob = await deps.github.get_blob(
            repository.installation_id, repository.full_name, change.blob_sha
        )
        text = blob.decode("utf-8", errors="replace")
        analysis = analyze_source(text, change.path)
        head.analyses[change.path] = analysis
        head.sources[change.path] = text
        head.hashes.update(s.body_hash for s in analysis.symbols if s.kind != "class")
        hit = touched_symbols(list(analysis.symbols), change.patch, analysis.declarations)
        if hit:
            head.touched[change.path] = hit
    return head


async def _changed_symbol_ids(
    session: AsyncSession,
    repository_id: uuid.UUID,
    files: list[ChangedFile],
    head: _Head,
) -> tuple[set[uuid.UUID], dict[uuid.UUID, str]]:
    """Ids of the stored (default-branch) symbols the pull request touches or deletes. A symbol
    whose code reappears unchanged elsewhere in the PR is a rename/move: the rule follows it
    (resync), so it is not a candidate."""
    paths = {c.path for c in files} | {c.previous_path for c in files if c.previous_path}
    rows = (
        await session.scalars(
            select(SymbolRow).where(
                SymbolRow.repository_id == repository_id,
                SymbolRow.status == "active",
                SymbolRow.file_path.in_(paths),
            )
        )
    ).all()
    stored = {(r.file_path, r.qualified_name): r for r in rows}
    key_by_id = {r.id: _key(r.file_path, r.qualified_name) for r in rows}

    touched_ids = {
        stored[(path, s.qualified_name)].id
        for path, symbols in head.touched.items()
        for s in symbols
        if (path, s.qualified_name) in stored
    }
    head_names = {(path, s.qualified_name) for path, a in head.analyses.items() for s in a.symbols}
    removed_ids = {
        row.id
        for key, row in stored.items()
        if key not in head_names and row.body_hash not in head.hashes
    }
    return touched_ids | removed_ids, key_by_id


# --------------------------------------------------------------------------- candidates


async def _candidate_rules(
    session: AsyncSession,
    repository_id: uuid.UUID,
    direct_ids: set[uuid.UUID],
    key_by_id: dict[uuid.UUID, str],
    limit: int,
) -> list[_Candidate]:
    if not direct_ids:
        return []
    direct = await _rules_anchored_to(session, repository_id, direct_ids)
    candidates: list[_Candidate] = []
    for rule, anchors in direct.values():
        touched = [
            key_by_id.get(a.id, _key(a.file_path, a.qualified_name))
            for a in anchors
            if a.id in direct_ids
        ]
        copies = await _unchanged_copies(session, rule.id, direct_ids)
        candidates.append(_Candidate(rule, "direct", touched, copies))

    neighbor_ids = await _neighbors(session, direct_ids)
    related = await _rules_anchored_to(session, repository_id, neighbor_ids - direct_ids)
    candidates += [
        _Candidate(rule, "related", [_key(a.file_path, a.qualified_name) for a in anchors])
        for rule_id, (rule, anchors) in related.items()
        if rule_id not in direct
    ]
    return _merge_near_duplicates(candidates)[:limit]  # direct ones come first


async def _rules_anchored_to(
    session: AsyncSession, repository_id: uuid.UUID, symbol_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[Rule, list[SymbolRow]]]:
    if not symbol_ids:
        return {}
    rows = await session.execute(
        select(Rule, SymbolRow)
        .join(RuleAnchor, RuleAnchor.rule_id == Rule.id)
        .join(SymbolRow, SymbolRow.id == RuleAnchor.symbol_id)
        .where(
            Rule.repository_id == repository_id,
            Rule.status == "active",
            RuleAnchor.symbol_id.in_(symbol_ids),
        )
        .order_by(Rule.created_at)
    )
    grouped: dict[uuid.UUID, tuple[Rule, list[SymbolRow]]] = {}
    for rule, symbol in rows:
        grouped.setdefault(rule.id, (rule, []))[1].append(symbol)
    return grouped


async def _unchanged_copies(
    session: AsyncSession, rule_id: uuid.UUID, changed_ids: set[uuid.UUID]
) -> list[str]:
    """Other implementations of the same logic that this pull request does not touch.

    Anchors have a role: `primary` is where the rule lives, `duplicate` is a copy of that code.
    A rule can also be anchored to several primary symbols that are different code (a validator
    and the handler that calls it); those are not copies and must not be presented as such. So:
    when the change touches a copy, every other anchor is the logic it should agree with; when it
    touches a primary symbol, only the untouched copies count.
    """
    rows = (
        await session.execute(
            select(SymbolRow.id, SymbolRow.file_path, SymbolRow.qualified_name, RuleAnchor.role)
            .join(RuleAnchor, RuleAnchor.symbol_id == SymbolRow.id)
            .where(RuleAnchor.rule_id == rule_id, SymbolRow.status == "active")
        )
    ).all()
    touches_a_copy = any(role == "duplicate" for sid, _, _, role in rows if sid in changed_ids)
    return sorted(
        _key(path, name)
        for sid, path, name, role in rows
        if sid not in changed_ids and (touches_a_copy or role == "duplicate")
    )


def _cosine(a: Any, b: Any) -> float:
    x, y = [float(v) for v in a], [float(v) for v in b]
    dot = sum(p * q for p, q in zip(x, y, strict=True))
    norm = (sum(p * p for p in x) * sum(q * q for q in y)) ** 0.5
    return dot / norm if norm else 0.0


def _merge_near_duplicates(candidates: list[_Candidate]) -> list[_Candidate]:
    """Fold rules that say the same thing about the same code into the best formulation.

    Extraction can store one rule in several phrasings. Assessing and quoting all of them wastes
    model calls and, worse, a warning may quote a phrasing that has no rationale while another
    one has it. Candidates are merged only when they have the same relation, share a file and
    their statements are near-identical in embedding space; the kept one is the one that has a
    rationale, then the most confident.
    """
    best_first = sorted(
        range(len(candidates)),
        key=lambda i: (
            candidates[i].rule.rationale is not None,
            candidates[i].rule.confidence,
            -i,
        ),
        reverse=True,
    )
    kept: list[int] = []
    for index in best_first:
        candidate = candidates[index]
        host = next((k for k in kept if _same_rule(candidates[k], candidate)), None)
        if host is None:
            kept.append(index)
            continue
        winner = candidates[host]
        winner.merged_rule_ids.append(str(candidate.rule.id))
        winner.touched_keys += [k for k in candidate.touched_keys if k not in winner.touched_keys]
        winner.unchanged_duplicates += [
            k for k in candidate.unchanged_duplicates if k not in winner.unchanged_duplicates
        ]
        logger.info(
            "rule %s folded into %s (same rule, different wording)",
            candidate.rule.id,
            winner.rule.id,
        )
    return [candidates[i] for i in sorted(kept)]


def _same_rule(a: _Candidate, b: _Candidate) -> bool:
    if a.relation != b.relation or a.rule.embedding is None or b.rule.embedding is None:
        return False
    files_a = {k.partition("::")[0] for k in a.touched_keys}
    if not files_a & {k.partition("::")[0] for k in b.touched_keys}:
        return False
    return _cosine(a.rule.embedding, b.rule.embedding) >= RULE_MERGE_SIMILARITY


async def _neighbors(session: AsyncSession, start: set[uuid.UUID]) -> set[uuid.UUID]:
    """Symbols within RELATED_DEPTH calls of `start`, callers and callees alike."""
    seen = set(start)
    frontier = deque((symbol_id, 0) for symbol_id in start)
    while frontier:
        batch: list[uuid.UUID] = []
        depth = frontier[0][1]
        while frontier and frontier[0][1] == depth:
            batch.append(frontier.popleft()[0])
        if depth >= RELATED_DEPTH:
            continue
        rows = await session.execute(
            select(SymbolCall.caller_id, SymbolCall.callee_id).where(
                or_(SymbolCall.caller_id.in_(batch), SymbolCall.callee_id.in_(batch))
            )
        )
        for caller, callee in rows:
            for other in (caller, callee):
                if other not in seen:
                    seen.add(other)
                    frontier.append((other, depth + 1))
    return seen - start


# --------------------------------------------------------------------------- assessment


async def _assess(
    deps: WorkerDeps,
    llm: LLMProvider,
    repository_id: uuid.UUID,
    number: int,
    candidates: list[_Candidate],
    files: list[ChangedFile],
    head: _Head,
) -> list[tuple[_Candidate, str, ImpactAssessment]]:
    gate = asyncio.Semaphore(deps.impact.impact_concurrency)
    metadata = {"repository_id": str(repository_id), "pull_request": number}
    quota_errors: list[LLMQuotaError] = []

    async def one(candidate: _Candidate) -> tuple[_Candidate, str, ImpactAssessment] | None:
        prompt = build_impact_prompt(_context(candidate, files, head))
        async with gate:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    assessment = await llm.extract(
                        prompt, ImpactAssessment, purpose="impact", metadata=metadata
                    )
                except LLMQuotaError as error:
                    quota_errors.append(error)  # reported once, after the others have finished
                    return None
                except LLMTransientError:
                    raise  # the job retries later with backoff
                except (LLMError, ValidationError) as error:
                    logger.warning(
                        "impact attempt %d/%d failed for rule %s: %s",
                        attempt,
                        ATTEMPTS,
                        candidate.rule.id,
                        error,
                    )
                    continue
                return candidate, candidate.relation, _sanitize(assessment)
        return None

    done = await asyncio.gather(*(one(c) for c in candidates))
    if quota_errors:
        raise quota_errors[0]
    return [d for d in done if d is not None]


def _sanitize(assessment: ImpactAssessment) -> ImpactAssessment:
    """Clamp the confidence and enforce the evidence rules: a risk needs a real change of
    behaviour and a concrete example of it, otherwise the rule is simply not affected."""
    confidence = min(1.0, max(0.0, assessment.confidence))
    explanation = assessment.explanation.strip()
    example = (assessment.counterexample or "").strip() or None
    if not assessment.behavior_changed or example is None or not explanation:
        return ImpactAssessment(
            behavior_changed=False,
            counterexample=None,
            risk="none",
            confidence=confidence,
            explanation=explanation,
        )
    return ImpactAssessment(
        behavior_changed=True,
        counterexample=example,
        risk=assessment.risk,
        confidence=confidence,
        explanation=explanation,
    )


def _context(candidate: _Candidate, files: list[ChangedFile], head: _Head) -> ImpactContext:
    touched_keys = set(candidate.touched_keys)
    anchored: list[PromptSymbol] = []
    relevant_paths: list[str] = []
    for key in candidate.touched_keys:
        path, _, name = key.partition("::")
        symbol = next(
            (
                s
                for s in head.analyses.get(path, FileAnalysis(path, ())).symbols
                if s.qualified_name == name
            ),
            None,
        )
        if candidate.relation == "direct" and symbol is not None:
            anchored.append(
                PromptSymbol(key, symbol.kind, symbol_source(head.sources[path], symbol))
            )
        elif candidate.relation == "direct":
            anchored.append(PromptSymbol(key, "symbol", "(deleted or renamed by this change)"))
        else:
            anchored.append(
                PromptSymbol(key, "symbol", "(not changed by this PR; related by calls)")
            )
        relevant_paths.append(path)

    changed: list[PromptSymbol] = []
    if candidate.relation == "related":
        for path, symbols in head.touched.items():
            for symbol in symbols:
                if len(changed) < MAX_RELATED_CHANGED_SYMBOLS:
                    changed.append(
                        PromptSymbol(
                            _key(path, symbol.qualified_name),
                            symbol.kind,
                            symbol_source(head.sources[path], symbol),
                        )
                    )
    ordered = sorted(
        (c for c in files if c.patch),
        key=lambda c: (c.path not in relevant_paths, c.path),
    )
    return ImpactContext(
        statement=candidate.rule.statement,
        rationale=candidate.rule.rationale,
        relation="direct" if candidate.relation == "direct" else "related",
        anchored_symbols=anchored,
        changed_symbols=changed,
        diffs=[PromptFileDiff(c.path, c.patch or "") for c in ordered],
        unchanged_duplicates=[k for k in candidate.unchanged_duplicates if k not in touched_keys],
    )


# --------------------------------------------------------------------------- publishing


async def _publish(
    deps: WorkerDeps,
    repository: Repository,
    number: int,
    head_sha: str,
    assessments: list[tuple[_Candidate, str, ImpactAssessment]],
    reported: list[tuple[_Candidate, str, ImpactAssessment]],
    result: ImpactResult,
) -> None:
    language = deps.impact.impact_comment_language
    async with deps.session_factory() as session:
        previous = await session.scalar(
            select(PrAnalysis).where(
                PrAnalysis.repository_id == repository.id, PrAnalysis.pr_number == number
            )
        )
        findings = [await _finding(session, c, a) for c, _, a in reported]
    previously_flagged = bool(previous and any(f.get("reported") for f in previous.findings))

    comment_id = previous.comment_id if previous else None
    if findings:
        body = render_comment(findings, language)
        comment_id = await _post_or_update(deps, repository, number, comment_id, body)
        result.commented = True
    elif comment_id is not None and previously_flagged:
        body = render_all_clear(head_sha, language)
        comment_id = await _post_or_update(deps, repository, number, comment_id, body)
        result.commented = True
    result.comment_id = comment_id

    stored: list[dict[str, Any]] = [
        {
            "rule_id": str(c.rule.id),
            "relation": relation,
            "risk": _effective_risk(c, a),
            "confidence": a.confidence,
            "explanation": a.explanation,
            "counterexample": a.counterexample,
            "reported": (c, relation, a) in reported,
            "merged_rule_ids": c.merged_rule_ids,
        }
        for c, relation, a in assessments
    ]
    async with deps.session_factory() as session, session.begin():
        statement = insert(PrAnalysis).values(
            repository_id=repository.id,
            pr_number=number,
            head_sha=head_sha,
            comment_id=comment_id,
            findings=stored,
        )
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_pr_analyses_repo_pr",
                set_={
                    "head_sha": statement.excluded.head_sha,
                    "comment_id": statement.excluded.comment_id,
                    "findings": statement.excluded.findings,
                    "created_at": datetime.now(UTC),
                },
            )
        )


async def _post_or_update(
    deps: WorkerDeps,
    repository: Repository,
    number: int,
    comment_id: int | None,
    body: str,
) -> int:
    if comment_id is not None:
        try:
            await deps.github.update_comment(
                repository.installation_id, repository.full_name, comment_id, body
            )
            return comment_id
        except CommentNotFoundError:
            pass  # someone deleted it: post a fresh one
    return await deps.github.create_comment(
        repository.installation_id, repository.full_name, number, body
    )


async def _finding(
    session: AsyncSession,
    candidate: _Candidate,
    assessment: ImpactAssessment,
) -> Finding:
    rule = candidate.rule
    origin = (
        await session.execute(
            select(RuleOrigin.commit_sha, Commit.committed_at)
            .outerjoin(
                Commit,
                (Commit.repository_id == rule.repository_id)
                & (Commit.sha == RuleOrigin.commit_sha),
            )
            .where(RuleOrigin.rule_id == rule.id)
            .order_by(Commit.committed_at.asc().nulls_last())
            .limit(1)
        )
    ).first()
    risk = _effective_risk(candidate, assessment)
    copies = [k.partition("::")[0] for k in candidate.unchanged_duplicates]
    return Finding(
        rule_id=str(rule.id),
        statement=rule.statement,
        rationale=rule.rationale,
        risk=risk,
        confidence=assessment.confidence,
        explanation=assessment.explanation,
        unchanged_duplicates=sorted(set(copies)) if candidate.unchanged_duplicates else [],
        origin_commit=origin[0] if origin else None,
        origin_date=origin[1] if origin else None,
    )
