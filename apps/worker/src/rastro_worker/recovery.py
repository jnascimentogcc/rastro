"""Put back the jobs of workers that died.

Procrastinate marks a job `doing` when a worker takes it and only that worker can finish it. If
the process disappears (segfault, OOM kill, deploy restart, host reboot) the job stays `doing`
forever, and because the jobs of a repository share a lock, every later job of that repository
stays `todo`. Workers write a heartbeat; `recover_stalled_jobs` (periodic task) looks for `doing`
jobs whose worker has not sent one for `worker_stalled_timeout_seconds` and retries them. Procrastinate
itself only deletes the dead worker rows, it never retries their jobs.

A job gets at most `job_max_attempts` runs (`attempts` counts every retry, transient ones too);
after that it is marked `failed`, which also frees the repository's lock, so one job that kills its
worker every time cannot starve the repository.
"""

import datetime
import logging
from dataclasses import dataclass, field

from procrastinate import App
from procrastinate.jobs import Job, Status

logger = logging.getLogger(__name__)


@dataclass
class RecoveryReport:
    retried: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def _repository_of(job: Job) -> str:
    return str(job.lock or job.task_kwargs.get("repository_id") or "-")


async def recover_stalled_jobs(
    app: App, *, stalled_timeout: float, max_attempts: int, queue: str | None = None
) -> RecoveryReport:
    manager = app.job_manager
    report = RecoveryReport()
    for job in await manager.get_stalled_jobs(queue=queue, seconds_since_heartbeat=stalled_timeout):
        assert job.id is not None
        who = f"{job.task_name}[job {job.id}] of repository {_repository_of(job)}"
        if job.attempts + 1 >= max_attempts:
            logger.error(
                "%s was running on a worker that died and already ran %d time(s); giving up, "
                "marking it failed (max attempts %d). Its repository is unblocked.",
                who,
                job.attempts + 1,
                max_attempts,
            )
            await manager.finish_job_by_id_async(job.id, Status.FAILED, delete_job=False)
            report.failed.append(job.id)
            continue
        logger.warning(
            "%s was running on a worker that stopped sending heartbeats (died?); "
            "putting it back in the queue, attempt %d/%d",
            who,
            job.attempts + 2,
            max_attempts,
        )
        await manager.retry_job_by_id_async(job.id, datetime.datetime.now(datetime.UTC))
        report.retried.append(job.id)
    await manager.prune_stalled_workers(stalled_timeout)
    return report
