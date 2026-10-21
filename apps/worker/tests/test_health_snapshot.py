"""Phase 11.5: the daily snapshot job and its fan-out."""

import uuid

import pytest
from sqlalchemy import select, update
from worker_support import WorkerContext

from rastro_db.models import HealthSnapshot, Repository
from rastro_worker import health_snapshot

pytestmark = pytest.mark.db


async def snapshots(worker: WorkerContext, repository_id: uuid.UUID) -> list[HealthSnapshot]:
    async with worker.deps.session_factory() as session:
        return list(
            await session.scalars(
                select(HealthSnapshot).where(HealthSnapshot.repository_id == repository_id)
            )
        )


async def set_status(worker: WorkerContext, status: str) -> None:
    async with worker.deps.session_factory() as session, session.begin():
        await session.execute(
            update(Repository).where(Repository.id == worker.repository_id).values(status=status)
        )


async def test_a_snapshot_is_stored_for_an_active_repository(worker: WorkerContext) -> None:
    assert await health_snapshot.snapshot_repository(worker.deps, worker.repository_id) is True

    [row] = await snapshots(worker, worker.repository_id)
    assert "R3" in row.metrics


async def test_a_repository_that_is_not_active_gets_no_snapshot(worker: WorkerContext) -> None:
    await set_status(worker, "suspended")

    assert await health_snapshot.snapshot_repository(worker.deps, worker.repository_id) is False
    assert await snapshots(worker, worker.repository_id) == []


async def test_a_repository_that_cannot_be_queued_does_not_stop_the_others(
    worker: WorkerContext,
) -> None:
    queued: list[str] = []

    async def enqueue(repository_id: str) -> None:
        if repository_id == str(worker.repository_id):
            raise RuntimeError("queue unavailable")
        queued.append(repository_id)

    ok, failed = await health_snapshot.fan_out(worker.deps, enqueue)

    assert failed >= 1 and str(worker.repository_id) not in queued
    assert ok == len(queued)


async def test_only_active_repositories_are_queued(worker: WorkerContext) -> None:
    await set_status(worker, "uninstalled")
    queued: list[str] = []

    async def enqueue(repository_id: str) -> None:
        queued.append(repository_id)

    await health_snapshot.fan_out(worker.deps, enqueue)

    assert str(worker.repository_id) not in queued
