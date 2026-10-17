from datetime import UTC, datetime

from procrastinate import testing
from sqlalchemy import update
from worker_support import WorkerContext

from rastro_db.models import Commit
from rastro_worker.app import build_app
from rastro_worker.requeue import requeue_skipped_commits

SKIPPED_QUOTA, SKIPPED_BUDGET, ANALYZED, FAILED = "a" * 40, "b" * 40, "c" * 40, "d" * 40


async def commits_with_statuses(worker: WorkerContext) -> None:
    for sha, status in (
        (SKIPPED_QUOTA, "skipped_quota"),
        (SKIPPED_BUDGET, "skipped_budget"),
        (ANALYZED, "analyzed"),
        (FAILED, "analysis_failed"),
    ):
        await worker.add_commit_row(sha)
        async with worker.deps.session_factory() as session, session.begin():
            await session.execute(
                update(Commit)
                .where(Commit.repository_id == worker.repository_id, Commit.sha == sha)
                .values(analysis_status=status, committed_at=datetime(2026, 10, 1, tzinfo=UTC))
            )


async def test_skipped_commits_are_queued_again_with_force(worker: WorkerContext) -> None:
    await commits_with_statuses(worker)
    connector = testing.InMemoryConnector()
    queue = build_app(connector)

    async with queue.open_async():
        count = await requeue_skipped_commits(worker.deps.session_factory, queue)

    jobs = [j for j in connector.jobs.values() if j["task_name"] == "rule_extraction"]
    assert count == 2
    assert {j["args"]["sha"] for j in jobs} == {SKIPPED_QUOTA, SKIPPED_BUDGET}
    assert all(j["args"]["force"] is True for j in jobs)
    assert all(j["lock"] == str(worker.repository_id) for j in jobs)


async def test_statuses_and_repository_can_be_chosen(worker: WorkerContext) -> None:
    import uuid

    await commits_with_statuses(worker)
    connector = testing.InMemoryConnector()
    queue = build_app(connector)

    async with queue.open_async():
        failed = await requeue_skipped_commits(
            worker.deps.session_factory, queue, statuses=("analysis_failed",)
        )
        other_repository = await requeue_skipped_commits(
            worker.deps.session_factory, queue, repository_id=uuid.uuid4()
        )
        this_repository = await requeue_skipped_commits(
            worker.deps.session_factory, queue, repository_id=worker.repository_id
        )

    assert (failed, other_repository, this_repository) == (1, 0, 2)
