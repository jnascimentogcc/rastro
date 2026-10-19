"""Rule preview for a pull request (phase 9.2, spec `docs/spec-fase-9-validacao-humana.md`).

Rules are extracted only after a merge, so a reviewer used to approve a PR without seeing what it
teaches Rastro. This runs the extraction in preview mode over the PR's commits that have a captured
session and keeps the result in `rule_proposals`. Nothing reaches `rules`: if the PR is never
merged, the knowledge base is untouched.

- Each commit is previewed once (`rule_previews`); a push only pays for its new commits, and a
  session that arrives later re-runs just that commit.
- Several commits of one PR may propose the same rule: the latest wins, earlier ones that share a
  symbol become `superseded` (mirrors "last commit wins" at merge time).
- Budget and quota are respected; when the preview cannot run, the outcome says so, so silence is
  never read as "no rules".
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from rastro_db.models import AgentSession, Commit, Repository, Rule, RulePreview, RuleProposal
from rastro_llm import LLMProvider, LLMQuotaError
from rastro_worker.deps import WorkerDeps
from rastro_worker.retry import is_transient
from rastro_worker.rule_extraction import Proposal, propose_rules

logger = logging.getLogger(__name__)

OPEN_STATES = ("pending", "superseded")  # the only states 9.2 produces; later phases add more


@dataclass
class PreviewItem:
    key: str
    action: str  # create | update
    statement: str
    rationale: str | None
    symbol: str  # path::qualified.name of the first anchor
    confidence: float
    replaces: str | None = None  # statement of the rule an update changes
    validated_by: str | None = None  # set when that rule had been confirmed by a human
    validated_at: datetime | None = None


@dataclass
class PreviewOutcome:
    items: list[PreviewItem] = field(default_factory=list)
    unavailable: str | None = None  # budget | quota: the preview could not run for new commits
    previewed: int = 0  # commits sent to the model in this run


def proposal_key(statement: str, symbols: list[str]) -> str:
    text = " ".join(statement.lower().split()) + "|" + "|".join(sorted(symbols))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


async def refresh_preview(
    deps: WorkerDeps, llm: LLMProvider, repository: Repository, number: int
) -> PreviewOutcome:
    """Preview the PR's new commits, tidy the stored proposals and return what is still pending."""
    shas = list(
        dict.fromkeys(
            await deps.github.get_pull_request_commits(
                repository.installation_id, repository.full_name, number
            )
        )
    )
    outcome = PreviewOutcome()
    async with deps.session_factory() as session:
        sessions = list(
            await session.scalars(
                select(AgentSession)
                .where(
                    AgentSession.repository_id == repository.id, AgentSession.commit_sha.in_(shas)
                )
                .order_by(AgentSession.captured_at)
            )
        )
        previewed = dict(
            (
                await session.execute(
                    select(RulePreview.commit_sha, RulePreview.previewed_at).where(
                        RulePreview.repository_id == repository.id,
                        RulePreview.commit_sha.in_(shas),
                    )
                )
            ).all()
        )
        messages = dict(
            (
                await session.execute(
                    select(Commit.sha, Commit.message).where(
                        Commit.repository_id == repository.id, Commit.sha.in_(shas)
                    )
                )
            ).all()
        )
    by_sha: dict[str, list[AgentSession]] = {}
    for stored in sessions:
        by_sha.setdefault(stored.commit_sha, []).append(stored)

    for sha in shas:
        its_sessions = by_sha.get(sha)
        if not its_sessions:
            continue
        done_at = previewed.get(sha)
        if done_at is not None and all(s.captured_at <= done_at for s in its_sessions):
            continue  # cached
        if outcome.unavailable:
            continue
        if await deps.over_budget(repository.id):
            outcome.unavailable = "budget"
            continue
        try:
            proposals = await propose_rules(
                deps, llm, repository, sha, its_sessions, messages.get(sha) or ""
            )
        except LLMQuotaError as error:
            logger.error("rule preview for PR #%d skipped: no LLM credit left (%s)", number, error)
            outcome.unavailable = "quota"
            continue
        except Exception as error:
            if is_transient(error):
                raise  # the job retries later with backoff
            # e.g. the commit's refs are gone: the PR comment must still be published
            logger.warning("rule preview of %s skipped: %s", sha[:7], error)
            continue
        if proposals is None:
            continue  # model failure: not cached, tried again on the next push
        await _store(deps, repository.id, number, sha, proposals)
        outcome.previewed += 1

    await _tidy(deps, repository.id, number, shas)
    outcome.items = await _pending_items(deps, repository.id, number)
    return outcome


async def _store(
    deps: WorkerDeps, repository_id: uuid.UUID, number: int, sha: str, proposals: list[Proposal]
) -> None:
    async with deps.session_factory() as session, session.begin():
        await session.execute(
            delete(RuleProposal).where(
                RuleProposal.repository_id == repository_id,
                RuleProposal.commit_sha == sha,
                RuleProposal.state.in_(OPEN_STATES),
            )
        )
        rows = {
            proposal_key(p.statement, p.symbols): {
                "repository_id": repository_id,
                "pr_number": number,
                "commit_sha": sha,
                "proposal_key": proposal_key(p.statement, p.symbols),
                "action": p.action,
                "existing_rule_id": p.existing_rule_id,
                "statement": p.statement,
                "rationale": p.rationale,
                "origin_quote": p.origin_quote,
                "symbols": p.symbols,
                "confidence": p.confidence,
            }
            for p in proposals
        }
        if rows:
            statement = insert(RuleProposal).values(list(rows.values()))
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["repository_id", "commit_sha", "proposal_key"],
                    set_={
                        "pr_number": statement.excluded.pr_number,
                        "confidence": statement.excluded.confidence,
                        "rationale": statement.excluded.rationale,
                        "state": "pending",
                    },
                )
            )
        marker = insert(RulePreview).values(repository_id=repository_id, commit_sha=sha)
        await session.execute(
            marker.on_conflict_do_update(
                index_elements=["repository_id", "commit_sha"],
                set_={"previewed_at": datetime.now(UTC)},
            )
        )


async def _tidy(deps: WorkerDeps, repository_id: uuid.UUID, number: int, shas: list[str]) -> None:
    """Drop proposals of commits that left the PR and mark the ones a later commit replaces."""
    order = {sha: index for index, sha in enumerate(shas)}
    async with deps.session_factory() as session, session.begin():
        await session.execute(
            delete(RuleProposal).where(
                RuleProposal.repository_id == repository_id,
                RuleProposal.pr_number == number,
                RuleProposal.commit_sha.not_in(shas) if shas else RuleProposal.pr_number == number,
                RuleProposal.state.in_(OPEN_STATES),
            )
        )
        rows = list(
            await session.scalars(
                select(RuleProposal).where(
                    RuleProposal.repository_id == repository_id,
                    RuleProposal.pr_number == number,
                    RuleProposal.state.in_(OPEN_STATES),
                )
            )
        )
        for row in rows:
            later = [
                other
                for other in rows
                if order.get(other.commit_sha, -1) > order.get(row.commit_sha, -1)
                and set(other.symbols) & set(row.symbols)
            ]
            row.state = "superseded" if later else "pending"


async def _pending_items(
    deps: WorkerDeps, repository_id: uuid.UUID, number: int
) -> list[PreviewItem]:
    async with deps.session_factory() as session:
        rows = (
            await session.execute(
                select(RuleProposal, Rule)
                .outerjoin(Rule, Rule.id == RuleProposal.existing_rule_id)
                .where(
                    RuleProposal.repository_id == repository_id,
                    RuleProposal.pr_number == number,
                    RuleProposal.state == "pending",
                )
                .order_by(RuleProposal.created_at, RuleProposal.id)
            )
        ).all()
    items = []
    for proposal, rule in rows:
        validated = rule is not None and rule.validated_revision == rule.revision
        items.append(
            PreviewItem(
                key=proposal.proposal_key,
                action=proposal.action,
                statement=proposal.statement,
                rationale=proposal.rationale,
                symbol=proposal.symbols[0] if proposal.symbols else "",
                confidence=proposal.confidence,
                replaces=rule.statement if rule is not None else None,
                validated_by=rule.validated_by if validated and rule is not None else None,
                validated_at=rule.validated_at if validated and rule is not None else None,
            )
        )
    # Rules that overwrite something a human confirmed come first, then the most confident.
    items.sort(key=lambda item: (item.validated_by is None, -item.confidence))
    return items
