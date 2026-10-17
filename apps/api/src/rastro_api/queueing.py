import uuid
from typing import Any

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued


async def enqueue(
    queue: procrastinate.App,
    task: str,
    repository_id: uuid.UUID,
    *,
    queueing_lock: str | None = None,
    **arguments: Any,
) -> bool:
    """Queue a job. Jobs of one repository run one at a time (`lock`), so database updates cannot
    race. With `queueing_lock`, a job that is already waiting makes this call a no-op (returns
    False): use it when the waiting job will read the latest state anyway."""
    configured = queue.configure_task(
        name=task, lock=str(repository_id), queueing_lock=queueing_lock
    )
    try:
        await configured.defer_async(repository_id=str(repository_id), **arguments)
    except AlreadyEnqueued:
        return False
    return True
