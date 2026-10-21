"""The repository health report (phase 11, spec `docs/spec-fase-11-rastro-health.md`).

Counts over what Rastro already stores: SQL only, no model call, no text of conversations. Every
number comes with its numerator and denominator, and what cannot be known is `n/a` with the reason,
never 0% (spec D4). The API and the daily snapshot job (11.5) both call `build_report`, so they
cannot disagree.

Step 11.1 covers the knowledge base (R1-R4, R6, R3b by origin only), the pipeline warning (V2), the
zones and the review queue. Step 11.2 adds R3a (confirmed by someone other than the PR author), the
process figures P1-P5 and the source of intent V1, which need the PR author and the commit-to-PR link
that the webhook jobs started recording in migration 0012. Step 11.3 adds agent use (C1, C2) from `session_coverage`, computed when a capture arrives. R5 comes later.
"""

import fnmatch
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, and_, case, cast, distinct, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db.models import (
    AgentSession,
    Commit,
    Rule,
    RuleAnchor,
    RuleProposal,
    RuleReview,
    SessionCoverage,
    SymbolCall,
)
from rastro_db.models import Symbol as SymbolRow

DEFINITIONS_VERSION = 1
SMALL_SAMPLE = 10  # below this many cases a percentage is flagged, not trusted (spec D2)
STALE_AWAITING_MERGE_DAYS = 14
MIN_REVIEWERS = 3  # breakdowns that depend on who reviewed need this many distinct people (spec D3)
MAX_QUEUE = 50
DEFAULT_EXCLUDES = (
    "tests/*",
    "*/tests/*",
    "test/*",
    "*/test/*",
    "*__tests__*",
    "*.test.*",
    "*.spec.*",
    "*node_modules/*",
    "*.d.ts",
    "dist/*",
    "*/dist/*",
    "build/*",
    "*/build/*",
    "*generated*",
)
PIPELINE_PROBLEMS = ("analysis_failed", "skipped_budget", "skipped_quota")

# Shown by `rastro health --explain`: what each line is, where it comes from, what it does not prove.
DEFINITIONS: dict[str, dict[str, str]] = {
    "R1": {
        "title": "Rules by status",
        "formula": "count of rules grouped by status (active, orphaned, rejected, superseded)",
        "source": "rules.status",
        "not_proof": "That the active rules are complete: code with no rule is not counted",
    },
    "R2": {
        "title": "Active rules with a known reason",
        "formula": "active rules with a rationale / active rules, split by who wrote the reason",
        "source": "rules.rationale, rules.rationale_source",
        "not_proof": "That the recorded reason is correct",
    },
    "R3": {
        "title": "Active rules confirmed by a person",
        "formula": "active rules whose validated_revision equals their revision / active rules",
        "source": "rules.validated_revision, rules.revision",
        "not_proof": "That the person understood the rule",
    },
    "R3b": {
        "title": "Confirmations verified by GitHub",
        "formula": "validated rules whose validating review came from GitHub / validated rules",
        "source": "rule_reviews.reviewer_source",
        "not_proof": "Reviews made with the CLI carry an unverified name",
    },
    "R3a": {
        "title": "Confirmations by someone other than the PR author",
        "formula": (
            "validated rules whose validating review was made by a different GitHub login than the "
            "author of the pull request / validated rules whose validating review has both logins known"
        ),
        "source": "rule_reviews.reviewer_is_pr_author",
        "not_proof": (
            "Only GitHub reviews count: the CLI name is unverified, and reviews made before the PR author "
            "was recorded are unknown"
        ),
    },
    "R4": {
        "title": "Validation outdated by an edit of the rule",
        "formula": "active rules that were validated and then changed (validated_revision < revision)",
        "source": "rules.validated_revision, rules.revision",
        "not_proof": "",
    },
    "R6": {
        "title": "Oldest rules nobody confirmed",
        "formula": "age of the oldest active rule without a valid confirmation, and the median age",
        "source": "rules.created_at",
        "not_proof": "Age says how long it has waited, not how important it is",
    },
    "P1": {
        "title": "Pull requests with proposals",
        "formula": "distinct pull requests that received rule proposals in the window",
        "source": "rule_proposals",
        "not_proof": "A pull request without proposals may simply not have touched any rule",
    },
    "P2": {
        "title": "Proposals reviewed before the merge",
        "formula": "applied proposals created in the window that somebody reviewed / applied proposals",
        "source": "rule_proposals.reviewed_at",
        "not_proof": "That the review was careful",
    },
    "P3": {
        "title": "Proposals integrated without review",
        "formula": "applied proposals created in the window that nobody reviewed / applied proposals",
        "source": "rule_proposals.reviewed_at",
        "not_proof": "An unreviewed proposal may still be right; it was integrated unconfirmed",
    },
    "P4": {
        "title": "Time to the first review",
        "formula": "median of reviewed_at - created_at over the proposals reviewed in the window",
        "source": "rule_proposals",
        "not_proof": f"Shown only with at least {MIN_REVIEWERS} distinct reviewers, so it never points at a person",
    },
    "P5": {
        "title": "Perceived precision of the extraction",
        "formula": "verdicts in the window: confirmed as written / corrected / rejected",
        "source": "rule_reviews.verdict (the proposal's state is already 'applied' after the merge)",
        "not_proof": (
            "It tells how good the extraction is, not how good the people are. "
            f"Shown only with at least {MIN_REVIEWERS} distinct reviewers"
        ),
    },
    "C1": {
        "title": "Files with rules announced before they were edited",
        "formula": (
            "per session: files edited that already had rules when the session began and that the agent "
            "hook announced before the first edit / files edited that already had rules; summed over the "
            "sessions of the window whose hook state is known"
        ),
        "source": "session_coverage (computed when a capture arrives)",
        "not_proof": (
            "Only the files of the commit are counted. Edits made with Bash and no Read count as not "
            "announced, which is the gap this figure exists to show. Without the hook nothing is announced"
        ),
    },
    "C2": {
        "title": "Sessions where the hook ran",
        "formula": "sessions of the window whose session-start marker arrived / sessions with the state known",
        "source": "session_coverage.hook_active",
        "not_proof": 'It separates "nothing was announced" from "the hook did not run"',
    },
    "V1": {
        "title": "Integrations with no recorded intent",
        "formula": (
            "integrated pull requests, counted with all their commits and the merge or squash commit, "
            "that have no captured session / integrated pull requests plus direct commits with a session"
        ),
        "source": "commits.merged_pr_number, agent_sessions",
        "not_proof": (
            "Hand-written code has no session and is legitimate: this is code without recorded intent, "
            "not code generated by AI. Direct commits without a session cannot be told apart from older "
            "merge commits (before the PR link existed), so they are listed apart and left out of the ratio"
        ),
    },
    "V2": {
        "title": "Pipeline problems",
        "formula": (
            "commits in the window that failed analysis or were skipped for budget or quota, and "
            f"commits waiting for a merge for more than {STALE_AWAITING_MERGE_DAYS} days"
        ),
        "source": "commits.analysis_status",
        "not_proof": "While these exist the knowledge base may be out of date",
    },
    "zones": {
        "title": "Zones",
        "formula": (
            "per directory (depth 2): active symbols, symbols with a primary rule anchor, active "
            "rules anchored there, how many are confirmed and how many have no reason"
        ),
        "source": "symbols, rule_anchors, rules",
        "not_proof": "Much code (utilities, infrastructure) has no business rule: a low share is not bad",
    },
    "queue": {
        "title": "Review queue",
        "formula": (
            "active rules without a reason or without a valid confirmation, ordered by: no reason, "
            "not confirmed, number of callers of the anchor symbols, last change"
        ),
        "source": "rules, rule_anchors, symbol_calls",
        "not_proof": "The number of callers is a guess at importance",
    },
}


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    numerator: int | None = None
    denominator: int | None = None
    value: float | None = None  # numerator / denominator when both are known
    available: bool = True
    na_reason: str | None = None
    small_sample: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Zone:
    name: str
    symbols: int
    symbols_with_rule: int
    rules: int
    validated: int
    without_reason: int


@dataclass(frozen=True)
class QueueItem:
    rule_id: uuid.UUID
    statement: str
    reasons: list[str]  # no_rationale | not_validated | validation_outdated
    callers: int
    command: str  # how to act on it


@dataclass(frozen=True)
class HealthReport:
    repository_id: uuid.UUID
    generated_at: datetime
    window_days: int
    head_sha: str | None
    definitions_version: int
    warnings: list[str]
    pipeline: dict[str, int]
    knowledge: list[Metric]
    process: list[Metric]
    intent: list[Metric]
    agents: list[Metric]
    zones: list[Zone]
    queue: list[QueueItem]


def ratio(
    metric_id: str,
    label: str,
    numerator: int,
    denominator: int,
    *,
    na_reason: str,
    detail: dict[str, Any] | None = None,
) -> Metric:
    """A fraction that is `n/a` (never 0%) when there is nothing to divide by."""
    if denominator == 0:
        return Metric(metric_id, label, available=False, na_reason=na_reason, detail=detail or {})
    return Metric(
        metric_id,
        label,
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
        small_sample=denominator < SMALL_SAMPLE,
        detail=detail or {},
    )


def zone_of(path: str, depth: int = 2) -> str:
    """The first `depth` directories of a repository path; files in the root belong to `.`."""
    parts = path.replace("\\", "/").strip("/").split("/")[:-1]
    return "/".join(parts[:depth]) or "."


def is_excluded(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


async def build_report(
    session: AsyncSession,
    repository_id: uuid.UUID,
    *,
    window_days: int = 30,
    excludes: tuple[str, ...] | list[str] = (),
    zone_depth: int = 2,
    queue_size: int = 5,
    include_zones: bool = True,
    now: datetime | None = None,
) -> HealthReport:
    now = now or datetime.now(UTC)
    patterns = (*DEFAULT_EXCLUDES, *excludes)
    knowledge = await _knowledge(session, repository_id, now)
    window_start = now - timedelta(days=window_days)
    process = await _process(session, repository_id, window_start)
    intent = [await _intent(session, repository_id, window_start)]
    agents = await _agents(session, repository_id, window_start)
    pipeline = await _pipeline(session, repository_id, now, window_days)
    warnings = _warnings(pipeline)
    head_sha = await session.scalar(
        select(Commit.sha)
        .where(Commit.repository_id == repository_id, Commit.on_default_branch.is_(True))
        .order_by(Commit.committed_at.desc().nulls_last())
        .limit(1)
    )
    zones = await _zones(session, repository_id, patterns, zone_depth) if include_zones else []
    queue = await _queue(session, repository_id, min(max(queue_size, 0), MAX_QUEUE))
    return HealthReport(
        repository_id=repository_id,
        generated_at=now,
        window_days=window_days,
        head_sha=head_sha,
        definitions_version=DEFINITIONS_VERSION,
        warnings=warnings,
        pipeline=pipeline,
        knowledge=knowledge,
        process=process,
        intent=intent,
        agents=agents,
        zones=zones,
        queue=queue,
    )


# --------------------------------------------------------------------------- knowledge


def _is_validated() -> Any:
    return Rule.validated_revision == Rule.revision


async def _knowledge(
    session: AsyncSession, repository_id: uuid.UUID, now: datetime
) -> list[Metric]:
    by_status = dict(
        (
            await session.execute(
                select(Rule.status, func.count())
                .where(Rule.repository_id == repository_id)
                .group_by(Rule.status)
            )
        ).all()
    )
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Rule.rationale.is_not(None)),
                func.count().filter(
                    and_(Rule.rationale.is_not(None), Rule.rationale_source == "human")
                ),
                func.count().filter(
                    and_(Rule.rationale.is_not(None), Rule.rationale_source == "conversation")
                ),
                func.count().filter(_is_validated()),
                func.count().filter(Rule.validated_revision < Rule.revision),
            ).where(Rule.repository_id == repository_id, Rule.status == "active")
        )
    ).one()
    active, reasoned, human, conversation, validated, outdated = (int(value) for value in row)
    none_active = "no active rules"

    github_verified = await _github_verified(session, repository_id)
    by_other, author_known = await _confirmed_by_other(session, repository_id)
    not_validated = await _unvalidated_ages(session, repository_id, now)

    metrics = [
        Metric(
            "R1",
            "Rules by status",
            numerator=active,
            detail={
                "active": active,
                "orphaned": int(by_status.get("orphaned", 0)),
                "rejected": int(by_status.get("rejected", 0)),
                "superseded": int(by_status.get("superseded", 0)),
            },
        ),
        ratio(
            "R2",
            "With a known reason",
            reasoned,
            active,
            na_reason=none_active,
            detail={"from_conversation": conversation, "written_by_people": human},
        ),
        ratio("R3", "Confirmed by a person", validated, active, na_reason=none_active),
        ratio(
            "R3b",
            "...verified by GitHub",
            github_verified,
            validated,
            na_reason="no confirmed rules",
            detail={"unverified": validated - github_verified},
        ),
        ratio(
            "R3a",
            "...by someone other than the PR author",
            by_other,
            author_known,
            na_reason="no confirmation with a known PR author",
            detail={"unknown": validated - author_known},
        ),
        ratio(
            "R4",
            "Confirmation outdated by an edit of the rule",
            outdated,
            validated + outdated,
            na_reason="no rule was ever confirmed",
        ),
        not_validated,
    ]
    return metrics


async def _github_verified(session: AsyncSession, repository_id: uuid.UUID) -> int:
    """Validated active rules whose validating review (same revision) came from GitHub."""
    review = exists().where(
        RuleReview.rule_id == Rule.id,
        RuleReview.rule_revision == Rule.validated_revision,
        RuleReview.verdict.in_(("confirmed", "corrected")),
        RuleReview.reviewer_source == "github",
    )
    count = await session.scalar(
        select(func.count()).where(
            Rule.repository_id == repository_id, Rule.status == "active", _is_validated(), review
        )
    )
    return int(count or 0)


async def _confirmed_by_other(session: AsyncSession, repository_id: uuid.UUID) -> tuple[int, int]:
    """(validated by a login other than the PR author, validated with both logins known)."""
    flag = (
        select(RuleReview.reviewer_is_pr_author)
        .where(
            RuleReview.rule_id == Rule.id,
            RuleReview.rule_revision == Rule.validated_revision,
            RuleReview.verdict.in_(("confirmed", "corrected")),
        )
        .order_by(RuleReview.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(
                func.count().filter(flag.is_(False)),
                func.count().filter(flag.is_not(None)),
            ).where(Rule.repository_id == repository_id, Rule.status == "active", _is_validated())
        )
    ).one()
    return int(row[0]), int(row[1])


async def _unvalidated_ages(
    session: AsyncSession, repository_id: uuid.UUID, now: datetime
) -> Metric:
    not_valid = or_(Rule.validated_revision.is_(None), Rule.validated_revision < Rule.revision)
    age_seconds = func.extract("epoch", literal(now, Rule.created_at.type) - Rule.created_at)
    row = (
        await session.execute(
            select(
                func.count(),
                func.max(age_seconds),
                func.percentile_cont(0.5).within_group(age_seconds),
            ).where(Rule.repository_id == repository_id, Rule.status == "active", not_valid)
        )
    ).one()
    count = int(row[0])
    if count == 0:
        return Metric(
            "R6",
            "Oldest rules nobody confirmed",
            available=False,
            na_reason="every active rule is confirmed",
        )
    return Metric(
        "R6",
        "Oldest rules nobody confirmed",
        numerator=count,
        detail={
            "oldest_days": int(float(row[1]) // 86400),
            "median_days": int(float(row[2]) // 86400),
        },
    )


# --------------------------------------------------------------------------- process (P) and intent (V1)


async def _process(
    session: AsyncSession, repository_id: uuid.UUID, window_start: datetime
) -> list[Metric]:
    in_window = and_(
        RuleProposal.repository_id == repository_id, RuleProposal.created_at >= window_start
    )
    prs = int(
        await session.scalar(select(func.count(distinct(RuleProposal.pr_number))).where(in_window))
        or 0
    )
    applied, reviewed = (
        int(value)
        for value in (
            await session.execute(
                select(
                    func.count(), func.count().filter(RuleProposal.reviewed_at.is_not(None))
                ).where(in_window, RuleProposal.state == "applied")
            )
        ).one()
    )
    none = "no proposal was integrated in the window"
    metrics = [
        Metric("P1", "Pull requests with proposals", numerator=prs),
        ratio("P2", "Reviewed before the merge", reviewed, applied, na_reason=none),
        ratio("P3", "Integrated without review", applied - reviewed, applied, na_reason=none),
    ]

    elapsed = func.extract("epoch", RuleProposal.reviewed_at - RuleProposal.created_at)
    count, reviewers, median = (
        await session.execute(
            select(
                func.count(),
                func.count(distinct(RuleProposal.reviewed_by)),
                func.percentile_cont(0.5).within_group(elapsed),
            ).where(in_window, RuleProposal.reviewed_at.is_not(None))
        )
    ).one()
    few = f"fewer than {MIN_REVIEWERS} distinct reviewers: a breakdown could point at a person"
    if reviewers < MIN_REVIEWERS:
        metrics.append(Metric("P4", "Time to the first review", available=False, na_reason=few))
    else:
        metrics.append(
            Metric(
                "P4",
                "Time to the first review",
                numerator=int(count),
                detail={"median_hours": round(float(median) / 3600, 1)},
            )
        )

    verdicts = ("confirmed", "corrected", "rejected")
    rows = (
        await session.execute(
            select(RuleReview.verdict, func.count(), func.count(distinct(RuleReview.reviewer)))
            .join(Rule, Rule.id == RuleReview.rule_id)
            .where(
                Rule.repository_id == repository_id,
                RuleReview.created_at >= window_start,
                RuleReview.verdict.in_(verdicts),
            )
            .group_by(RuleReview.verdict)
        )
    ).all()
    by_verdict = {verdict: int(n) for verdict, n, _ in rows}
    distinct_reviewers = int(
        await session.scalar(
            select(func.count(distinct(RuleReview.reviewer)))
            .join(Rule, Rule.id == RuleReview.rule_id)
            .where(
                Rule.repository_id == repository_id,
                RuleReview.created_at >= window_start,
                RuleReview.verdict.in_(verdicts),
            )
        )
        or 0
    )
    total = sum(by_verdict.values())
    if distinct_reviewers < MIN_REVIEWERS and total:
        metrics.append(Metric("P5", "Verdicts on the extraction", available=False, na_reason=few))
    else:
        metrics.append(
            ratio(
                "P5",
                "Confirmed as written",
                by_verdict.get("confirmed", 0),
                total,
                na_reason="no verdicts in the window",
                detail={verdict: by_verdict.get(verdict, 0) for verdict in verdicts},
            )
        )
    return metrics


async def _intent(
    session: AsyncSession, repository_id: uuid.UUID, window_start: datetime
) -> Metric:
    """V1 per integration unit: a pull request (all its commits and the merge or squash commit) or a
    direct commit. A unit is dated by its newest commit, because the PR's own commits are older than
    the merge that integrates them."""
    unit = case(
        (Commit.merged_pr_number.is_not(None), "pr:" + cast(Commit.merged_pr_number, Text)),
        else_="sha:" + Commit.sha,
    )
    has_session = exists().where(
        AgentSession.repository_id == Commit.repository_id, AgentSession.commit_sha == Commit.sha
    )
    units = (
        select(
            func.bool_or(Commit.merged_pr_number.is_not(None)).label("is_pr"),
            func.bool_or(has_session).label("has_session"),
            func.max(Commit.committed_at).label("newest"),
        )
        .where(Commit.repository_id == repository_id, Commit.on_default_branch.is_(True))
        .group_by(unit)
        .subquery()
    )
    row = (
        await session.execute(
            select(
                func.count().filter(and_(units.c.is_pr, units.c.newest >= window_start)),
                func.count().filter(
                    and_(units.c.is_pr, units.c.has_session, units.c.newest >= window_start)
                ),
                func.count().filter(
                    and_(~units.c.is_pr, units.c.has_session, units.c.newest >= window_start)
                ),
                func.count().filter(
                    and_(~units.c.is_pr, ~units.c.has_session, units.c.newest >= window_start)
                ),
                func.count().filter(units.c.newest.is_(None)),
            )
        )
    ).one()
    pr_units, pr_with_session, direct_with_session, unattributed, undated = (int(v) for v in row)
    detail = {
        "pull_requests": pr_units,
        "pull_requests_with_session": pr_with_session,
        "direct_commits_with_session": direct_with_session,
        "unattributed_commits": unattributed,
        "undated_units": undated,
    }
    return ratio(
        "V1",
        "Integrations with no recorded intent",
        pr_units - pr_with_session,
        pr_units + direct_with_session,
        na_reason="no integration in the window",
        detail=detail,
    )


# --------------------------------------------------------------------------- agent use (C)


async def _agents(
    session: AsyncSession, repository_id: uuid.UUID, window_start: datetime
) -> list[Metric]:
    cov = SessionCoverage
    known = cov.hook_active.is_not(None)
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(cov.hook_active.is_(True)),
                func.count().filter(known),
                func.coalesce(func.sum(cov.consulted_first).filter(known), 0),
                func.coalesce(func.sum(cov.edited_ruled).filter(known), 0),
                func.coalesce(func.sum(cov.consulted_first).filter(cov.hook_active.is_(True)), 0),
                func.coalesce(func.sum(cov.edited_ruled).filter(cov.hook_active.is_(True)), 0),
            ).where(cov.repository_id == repository_id, cov.started_at >= window_start)
        )
    ).one()
    total, with_hook, with_known, consulted, edited, consulted_hook, edited_hook = (
        int(v) for v in row
    )
    unknown = total - with_known
    if edited == 0:
        reason = (
            "the CLI did not say whether the hook ran in these sessions (older CLI)"
            if total and not with_known
            else "no session in the window edited a file that already had rules"
        )
    else:
        reason = ""
    c1 = ratio(
        "C1",
        "Files with rules announced before they were edited",
        consulted,
        edited,
        na_reason=reason,
        detail={
            "with_hook": {"announced": consulted_hook, "edited": edited_hook},
            "without_hook": {
                "announced": consulted - consulted_hook,
                "edited": edited - edited_hook,
            },
            "sessions_with_unknown_hook": unknown,
        },
    )
    c2 = ratio(
        "C2",
        "Sessions where the hook ran",
        with_hook,
        with_known,
        na_reason="no session in the window says whether the hook ran",
        detail={"unknown": unknown},
    )
    return [c1, c2]


# --------------------------------------------------------------------------- pipeline


async def _pipeline(
    session: AsyncSession, repository_id: uuid.UUID, now: datetime, window_days: int
) -> dict[str, int]:
    window_start = now - timedelta(days=window_days)
    stale_before = now - timedelta(days=STALE_AWAITING_MERGE_DAYS)
    rows = (
        await session.execute(
            select(Commit.analysis_status, func.count())
            .where(
                Commit.repository_id == repository_id,
                or_(
                    and_(
                        Commit.analysis_status.in_(PIPELINE_PROBLEMS),
                        Commit.committed_at >= window_start,
                    ),
                    and_(
                        Commit.analysis_status == "awaiting_merge",
                        Commit.committed_at < stale_before,
                    ),
                ),
            )
            .group_by(Commit.analysis_status)
        )
    ).all()
    counts = {status: int(count) for status, count in rows}
    return {status: counts.get(status, 0) for status in (*PIPELINE_PROBLEMS, "awaiting_merge")}


def _warnings(pipeline: dict[str, int]) -> list[str]:
    parts = []
    if pipeline["skipped_budget"] or pipeline["skipped_quota"]:
        parts.append(
            f"{pipeline['skipped_budget'] + pipeline['skipped_quota']} commit(s) were not analysed "
            "because of the cost budget or the model quota"
        )
    if pipeline["analysis_failed"]:
        parts.append(f"{pipeline['analysis_failed']} commit(s) failed analysis")
    if pipeline["awaiting_merge"]:
        parts.append(
            f"{pipeline['awaiting_merge']} commit(s) have waited for a merge for more than "
            f"{STALE_AWAITING_MERGE_DAYS} days"
        )
    if not parts:
        return []
    return ["; ".join(parts) + ": the numbers below may be out of date."]


# --------------------------------------------------------------------------- zones


async def _zones(
    session: AsyncSession,
    repository_id: uuid.UUID,
    patterns: tuple[str, ...] | list[str],
    depth: int,
) -> list[Zone]:
    active_symbol = and_(SymbolRow.repository_id == repository_id, SymbolRow.status == "active")
    symbol_rows = (
        await session.execute(
            select(SymbolRow.file_path, func.count())
            .where(active_symbol)
            .group_by(SymbolRow.file_path)
        )
    ).all()
    ruled_rows = (
        await session.execute(
            select(SymbolRow.file_path, func.count(distinct(SymbolRow.id)))
            .join(
                RuleAnchor, and_(RuleAnchor.symbol_id == SymbolRow.id, RuleAnchor.role == "primary")
            )
            .join(Rule, and_(Rule.id == RuleAnchor.rule_id, Rule.status == "active"))
            .where(active_symbol)
            .group_by(SymbolRow.file_path)
        )
    ).all()
    rule_rows = (
        await session.execute(
            select(
                SymbolRow.file_path,
                Rule.id,
                _is_validated().label("validated"),
                Rule.rationale.is_(None).label("without_reason"),
            )
            .join(RuleAnchor, RuleAnchor.symbol_id == SymbolRow.id)
            .join(Rule, and_(Rule.id == RuleAnchor.rule_id, Rule.status == "active"))
            .where(active_symbol)
            .distinct()
        )
    ).all()

    symbols: dict[str, int] = {}
    ruled: dict[str, int] = {}
    rules: dict[str, dict[uuid.UUID, tuple[bool, bool]]] = {}
    for path, count in symbol_rows:
        if not is_excluded(path, patterns):
            symbols[zone_of(path, depth)] = symbols.get(zone_of(path, depth), 0) + int(count)
    for path, count in ruled_rows:
        if not is_excluded(path, patterns):
            ruled[zone_of(path, depth)] = ruled.get(zone_of(path, depth), 0) + int(count)
    for path, rule_id, validated, without_reason in rule_rows:
        if not is_excluded(path, patterns):
            rules.setdefault(zone_of(path, depth), {})[rule_id] = (
                bool(validated),
                bool(without_reason),
            )

    zones = []
    for name in sorted(set(symbols) | set(rules)):
        found = rules.get(name, {})
        zones.append(
            Zone(
                name=name,
                symbols=symbols.get(name, 0),
                symbols_with_rule=ruled.get(name, 0),
                rules=len(found),
                validated=sum(1 for validated, _ in found.values() if validated),
                without_reason=sum(1 for _, without in found.values() if without),
            )
        )
    return zones


# --------------------------------------------------------------------------- queue


async def _queue(session: AsyncSession, repository_id: uuid.UUID, size: int) -> list[QueueItem]:
    if size == 0:
        return []
    # One grouped join for every rule (a correlated subquery per rule scans symbol_calls each time)
    callers_of = (
        select(
            RuleAnchor.rule_id.label("rule_id"),
            func.count(distinct(SymbolCall.caller_id)).label("callers"),
        )
        .join(SymbolCall, SymbolCall.callee_id == RuleAnchor.symbol_id)
        .join(Rule, and_(Rule.id == RuleAnchor.rule_id, Rule.repository_id == repository_id))
        .group_by(RuleAnchor.rule_id)
        .subquery()
    )
    callers = func.coalesce(callers_of.c.callers, 0)
    no_reason = Rule.rationale.is_(None)
    validated = _is_validated()
    rows = (
        await session.execute(
            select(
                Rule.id,
                Rule.statement,
                no_reason,
                validated,
                Rule.validated_revision.is_not(None),
                callers.label("callers"),
            )
            .outerjoin(callers_of, callers_of.c.rule_id == Rule.id)
            .where(
                Rule.repository_id == repository_id,
                Rule.status == "active",
                or_(no_reason, ~validated),
            )
            .order_by(
                no_reason.desc(),
                (~validated).desc(),
                callers.desc(),
                Rule.updated_at.desc(),
                Rule.id,
            )
            .limit(size)
        )
    ).all()
    queue = []
    for rule_id, statement, missing_reason, is_valid, was_validated, count in rows:
        reasons = []
        if missing_reason:
            reasons.append("no_rationale")
        if not is_valid:
            reasons.append("validation_outdated" if was_validated else "not_validated")
        queue.append(
            QueueItem(
                rule_id=rule_id,
                statement=statement,
                reasons=reasons,
                callers=int(count or 0),
                command=f"rastro rule {str(rule_id)[:6]} --confirm",
            )
        )
    return queue
