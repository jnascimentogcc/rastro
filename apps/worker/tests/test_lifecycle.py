import pytest
from sqlalchemy import update
from worker_support import WorkerContext

from rastro_db.models import Repository
from rastro_worker import tasks

pytestmark = pytest.mark.db


async def set_status(worker: WorkerContext, status: str) -> None:
    async with worker.deps.session_factory() as session, session.begin():
        await session.execute(
            update(Repository).where(Repository.id == worker.repository_id).values(status=status)
        )


@pytest.mark.parametrize("status", ["suspended", "uninstalled"])
async def test_jobs_of_inactive_repositories_do_nothing(worker: WorkerContext, status: str) -> None:
    await set_status(worker, status)
    repository_id = str(worker.repository_id)

    # The fake GitHub knows no trees, commits or pull requests: touching it would raise.
    await tasks.index_repository(repository_id)
    await tasks.analyze_commit(repository_id, "a" * 40, True)
    await tasks.extract_rules(repository_id, "a" * 40)
    await tasks.resync_anchors(repository_id)
    await tasks.analyze_pr(repository_id, 7, "b" * 40)

    assert worker.github.calls == []
    assert worker.llm.calls == []


async def test_active_repositories_are_processed(worker: WorkerContext) -> None:
    assert await tasks.repository_is_active(str(worker.repository_id)) is True
