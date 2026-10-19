"""Recovery of jobs orphaned by a dead worker, against the compose Postgres (real heartbeats)."""

import uuid
from collections.abc import AsyncIterator

import procrastinate
import psycopg
import pytest
from procrastinate.jobs import Job
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rastro_db import psycopg_conninfo
from rastro_worker.app import app as real_app
from rastro_worker.recovery import RecoveryReport, recover_stalled_jobs
from rastro_worker.tasks import ping

pytestmark = pytest.mark.db

TIMEOUT = 120.0


@pytest.fixture
async def queue_app() -> AsyncIterator[tuple[procrastinate.App, str]]:
    """The real app on a real connector, in a throwaway database: a worker that happens to be
    running against the shared dev database would otherwise pick the test jobs up."""
    admin_info = psycopg_conninfo()
    name = f"rastro_recovery_{uuid.uuid4().hex[:8]}"
    async with await psycopg.AsyncConnection.connect(admin_info, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    connector = procrastinate.PsycopgConnector(conninfo=make_conninfo(admin_info, dbname=name))
    try:
        with real_app.replace_connector(connector) as app:
            async with app.open_async():
                await app.schema_manager.apply_schema_async()
                yield app, "default"
    finally:
        async with await psycopg.AsyncConnection.connect(admin_info, autocommit=True) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


async def defer(app: procrastinate.App, queue: str, lock: str, message: str) -> int:
    return await ping.configure(queue=queue, lock=lock).defer_async(message=message)


async def status_of(app: procrastinate.App, job_id: int) -> str:
    jobs = list(await app.job_manager.list_jobs_async(id=job_id))
    return str(jobs[0].status)


async def take_as_new_worker(app: procrastinate.App, queue: str) -> tuple[int, Job]:
    """What a worker does before running a job: register, fetch (job -> doing)."""
    worker_id = await app.job_manager.register_worker()
    job = await app.job_manager.fetch_job([queue], worker_id)
    assert job is not None
    return worker_id, job


async def age_heartbeat(app: procrastinate.App, worker_id: int, seconds: int) -> None:
    await app.connector.execute_query_async(
        "UPDATE procrastinate_workers SET last_heartbeat = NOW() - make_interval(secs => %(s)s) "
        "WHERE id = %(id)s",
        s=seconds,
        id=worker_id,
    )


async def run_recovery(app: procrastinate.App, queue: str, max_attempts: int = 5) -> RecoveryReport:
    return await recover_stalled_jobs(
        app, stalled_timeout=TIMEOUT, max_attempts=max_attempts, queue=queue
    )


async def test_job_of_a_dead_worker_is_recovered_and_unblocks_the_repository(
    queue_app: tuple[procrastinate.App, str], caplog: pytest.LogCaptureFixture
) -> None:
    app, queue = queue_app
    lock = str(uuid.uuid4())
    first = await defer(app, queue, lock, "first")
    second = await defer(app, queue, lock, "second")  # same repository: waits behind the first

    dead_worker, job = await take_as_new_worker(app, queue)
    assert job.id == first
    assert await app.job_manager.fetch_job([queue], dead_worker) is None  # lock held: blocked
    await age_heartbeat(app, dead_worker, 3600)

    with caplog.at_level("WARNING", logger="rastro_worker.recovery"):
        report = await run_recovery(app, queue)

    assert report.retried == [first]
    assert report.failed == []
    assert await status_of(app, first) == "todo"
    warning = next(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert f"ping[job {first}]" in warning
    assert lock in warning  # the repository

    await app.run_worker_async(
        wait=False, queues=[queue], install_signal_handlers=False, concurrency=1
    )
    assert await status_of(app, first) == "succeeded"
    assert await status_of(app, second) == "succeeded"  # ran after recovery, in order


async def test_job_of_a_live_worker_is_left_alone(
    queue_app: tuple[procrastinate.App, str],
) -> None:
    app, queue = queue_app
    job_id = await defer(app, queue, str(uuid.uuid4()), "busy")
    live_worker, _ = await take_as_new_worker(app, queue)
    await age_heartbeat(app, live_worker, 30)  # quiet for a while, but well inside the timeout

    report = await run_recovery(app, queue)

    assert report.retried == [] and report.failed == []
    assert await status_of(app, job_id) == "doing"
    await app.job_manager.unregister_worker(live_worker)


async def test_worker_row_of_a_dead_worker_is_pruned(
    queue_app: tuple[procrastinate.App, str],
) -> None:
    app, queue = queue_app
    await defer(app, queue, str(uuid.uuid4()), "x")
    dead_worker, _ = await take_as_new_worker(app, queue)
    await age_heartbeat(app, dead_worker, 3600)

    await run_recovery(app, queue)

    row = await app.connector.execute_query_one_async(
        "SELECT count(*) AS n FROM procrastinate_workers WHERE id = %(id)s", id=dead_worker
    )
    assert row["n"] == 0


async def test_job_that_keeps_killing_its_worker_ends_failed_and_frees_the_lock(
    queue_app: tuple[procrastinate.App, str], caplog: pytest.LogCaptureFixture
) -> None:
    app, queue = queue_app
    lock = str(uuid.uuid4())
    poison = await defer(app, queue, lock, "poison")
    after = await defer(app, queue, lock, "after")
    max_attempts = 3

    for run in range(max_attempts):
        worker_id, job = await take_as_new_worker(app, queue)
        assert job.id == poison  # the retried job keeps its place; `after` waits on the lock
        assert job.attempts == run
        await age_heartbeat(app, worker_id, 3600)
        with caplog.at_level("ERROR", logger="rastro_worker.recovery"):
            report = await run_recovery(app, queue, max_attempts)

    assert report.failed == [poison]
    assert await status_of(app, poison) == "failed"
    assert any("giving up" in r.getMessage() for r in caplog.records)

    await app.run_worker_async(
        wait=False, queues=[queue], install_signal_handlers=False, concurrency=1
    )
    assert await status_of(app, after) == "succeeded"  # the repository is unblocked


def test_recovery_is_scheduled_every_minute() -> None:
    tasks = [p.task.name for p in real_app.periodic_registry.periodic_tasks.values()]
    assert "recover_stalled_jobs" in tasks
