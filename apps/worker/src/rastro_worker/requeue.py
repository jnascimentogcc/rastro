"""Queue rule extraction again for commits that were skipped for a reason that has gone away.

    uv run --env-file .env python -m rastro_worker.requeue [--status skipped_quota ...]

`skipped_quota` (the LLM account ran out of credit) and `skipped_budget` (the monthly token budget
was used up) mark commits that were NOT analysed; once credit or budget is back they need another
run. Each commit is queued with `force=True`, one at a time per repository (`lock`).
"""

import argparse
import asyncio
import sys
import uuid

import procrastinate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rastro_db import create_engine, create_session_factory
from rastro_db.models import Commit

DEFAULT_STATUSES = ("skipped_quota", "skipped_budget")


async def requeue_skipped_commits(
    session_factory: async_sessionmaker[AsyncSession],
    queue: procrastinate.App,
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    repository_id: uuid.UUID | None = None,
) -> int:
    """Defer `rule_extraction(force=True)` for every commit in one of `statuses`. Returns how
    many jobs were queued. The queue must be open (`async with queue.open_async()`)."""
    query = select(Commit.repository_id, Commit.sha).where(Commit.analysis_status.in_(statuses))
    if repository_id is not None:
        query = query.where(Commit.repository_id == repository_id)
    async with session_factory() as session:
        rows = (await session.execute(query.order_by(Commit.committed_at.asc().nulls_last()))).all()
    for repository, sha in rows:
        await queue.configure_task(name="rule_extraction", lock=str(repository)).defer_async(
            repository_id=str(repository), sha=sha, force=True
        )
    return len(rows)


async def main_async(statuses: tuple[str, ...], repository_id: uuid.UUID | None) -> int:
    from rastro_worker.app import build_app

    engine = create_engine()
    queue = build_app()
    try:
        async with queue.open_async():
            count = await requeue_skipped_commits(
                create_session_factory(engine),
                queue,
                statuses=statuses,
                repository_id=repository_id,
            )
    finally:
        await engine.dispose()
    print(f"Queued {count} commit(s) for rule extraction (statuses: {', '.join(statuses)}).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="rastro_worker.requeue")
    parser.add_argument(
        "--status",
        action="append",
        dest="statuses",
        help=f"commit analysis_status to re-run (repeatable; default: {', '.join(DEFAULT_STATUSES)})",
    )
    parser.add_argument("--repository-id", type=uuid.UUID, help="only this repository")
    args = parser.parse_args()
    statuses = tuple(args.statuses) if args.statuses else DEFAULT_STATUSES
    sys.exit(asyncio.run(main_async(statuses, args.repository_id)))


if __name__ == "__main__":
    main()
