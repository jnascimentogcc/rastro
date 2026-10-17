import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_impact import AGENDA_SATURDAY, AGENDA_V1, PR, seeded
from test_rule_extraction import AFTER, AGENDA, BEFORE, REQUEST, SHA2, rule
from worker_support import WorkerContext, user_excerpt

from rastro_db.budget import budget_exhausted, month_start, tokens_used_this_month
from rastro_db.models import Commit, LlmCall
from rastro_llm import ImpactAssessment, RuleExtraction
from rastro_worker import tasks
from rastro_worker.impact import analyze_pull_request
from rastro_worker.rule_extraction import extract_rules

pytestmark = pytest.mark.db


async def spend(
    worker: WorkerContext,
    tokens: int,
    *,
    when: datetime | None = None,
    repo: uuid.UUID | None = None,
) -> None:
    async with worker.deps.session_factory() as session, session.begin():
        call = LlmCall(
            repository_id=repo or worker.repository_id,
            purpose="extraction",
            model="m",
            input_tokens=tokens - 10,
            output_tokens=10,
        )
        if when is not None:
            call.created_at = when
        session.add(call)


async def stage_commit(worker: WorkerContext) -> None:
    worker.stage_commit(SHA2, {AGENDA: AFTER}, {AGENDA: BEFORE})
    await worker.add_commit_row(SHA2, "Block implants on Friday")
    await worker.add_session(SHA2, [user_excerpt(REQUEST)])


async def commit_status(worker: WorkerContext) -> str | None:
    async with worker.deps.session_factory() as session:
        return await session.scalar(
            select(Commit.analysis_status).where(
                Commit.repository_id == worker.repository_id, Commit.sha == SHA2
            )
        )


async def test_usage_counts_only_this_month_and_this_repository(worker: WorkerContext) -> None:
    this_month = month_start() + timedelta(seconds=1)
    await spend(worker, 400, when=this_month)
    await spend(worker, 300, when=month_start() - timedelta(days=2))  # last month: forgotten
    async with worker.deps.session_factory() as session:
        assert await tokens_used_this_month(session, worker.repository_id) == 400
        assert await tokens_used_this_month(session, uuid.uuid4()) == 0
        assert await budget_exhausted(session, worker.repository_id, 400) is True
        assert await budget_exhausted(session, worker.repository_id, 401) is False
        assert await budget_exhausted(session, worker.repository_id, None) is False
        assert await budget_exhausted(session, worker.repository_id, 0) is False  # unlimited


def test_month_boundary_is_utc() -> None:
    assert month_start(datetime(2026, 10, 31, 23, 59, tzinfo=UTC)) == datetime(
        2026, 10, 1, tzinfo=UTC
    )


async def test_extraction_is_skipped_with_a_clear_status_when_over_budget(
    worker: WorkerContext,
) -> None:
    await stage_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))
    worker.deps.monthly_token_budget = 1_000
    await spend(worker, 1_000)

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "skipped_budget"
    assert worker.llm.calls == [] and worker.github.calls == []  # not a single model call
    assert await commit_status(worker) == "skipped_budget"
    assert await worker.rules() == []


async def test_extraction_runs_again_once_there_is_budget(worker: WorkerContext) -> None:
    await stage_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))
    worker.deps.monthly_token_budget = 1_000
    await spend(worker, 1_000)
    await extract_rules(worker.deps, worker.repository_id, SHA2)

    worker.deps.monthly_token_budget = 5_000  # budget raised (or a new month began)
    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert (summary.status, summary.created) == ("analyzed", 1)
    assert await commit_status(worker) == "analyzed"


async def test_without_a_budget_nothing_is_limited(worker: WorkerContext) -> None:
    await stage_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))
    await spend(worker, 10_000_000)

    assert (await extract_rules(worker.deps, worker.repository_id, SHA2)).status == "analyzed"


async def test_commits_without_a_session_keep_their_own_status_over_budget(
    worker: WorkerContext,
) -> None:
    worker.deps.monthly_token_budget = 1
    await spend(worker, 100)
    await worker.add_commit_row(SHA2, "no session here")

    assert (await extract_rules(worker.deps, worker.repository_id, SHA2)).status == (
        "skipped_no_session"
    )


async def test_impact_analysis_is_skipped_without_comment_when_over_budget(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(
        ImpactAssessment(
            behavior_changed=False,
            counterexample=None,
            risk="none",
            confidence=0.9,
            explanation="-",
        )
    )
    worker.deps.monthly_token_budget = 50
    await spend(worker, 50)
    calls_before = len(worker.github.calls)

    result = await analyze_pull_request(worker.deps, worker.repository_id, PR, "a" * 40)

    assert result.status == "skipped_budget"
    assert worker.llm.calls == [] and worker.github.comments == {}
    assert not any("get_pull_request_files" in c for c in worker.github.calls[calls_before:])


async def test_the_job_wrapper_passes_the_budget_through(worker: WorkerContext) -> None:
    await stage_commit(worker)
    worker.deps.monthly_token_budget = 10
    await spend(worker, 10)

    await tasks.extract_rules(str(worker.repository_id), SHA2)

    assert await commit_status(worker) == "skipped_budget"
