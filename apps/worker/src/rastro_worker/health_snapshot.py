"""Daily snapshot of the health report (phase 11.5).

A periodic task fans out one `health_snapshot` job per active repository, so a repository whose
report fails (or whose job cannot be queued) never stops the others. No model call is involved.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select

from rastro_db import health_snapshots
from rastro_db.models import Repository
from rastro_worker.deps import WorkerDeps

logger = logging.getLogger(__name__)


async def snapshot_repository(
    deps: WorkerDeps, repository_id: uuid.UUID, *, now: datetime | None = None
) -> bool:
    """Store today's snapshot; False (and nothing written) when the repository is not active."""
    async with deps.session_factory() as session, session.begin():
        status = await session.scalar(
            select(Repository.status).where(Repository.id == repository_id)
        )
        if status != "active":
            return False
        await health_snapshots.take_snapshot(session, repository_id, now=now)
    return True


async def fan_out(deps: WorkerDeps, enqueue: Callable[[str], Awaitable[None]]) -> tuple[int, int]:
    """Queue a snapshot job for every active repository; returns (queued, failed)."""
    async with deps.session_factory() as session:
        ids = list(
            await session.scalars(select(Repository.id).where(Repository.status == "active"))
        )
    queued = failed = 0
    for repository_id in ids:
        try:
            await enqueue(str(repository_id))
            queued += 1
        except Exception:
            failed += 1
            logger.exception("could not queue the health snapshot of repository %s", repository_id)
    return queued, failed
