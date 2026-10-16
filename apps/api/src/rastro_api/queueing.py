import uuid
from typing import Any

import procrastinate


async def enqueue(
    queue: procrastinate.App, task: str, repository_id: uuid.UUID, **arguments: Any
) -> None:
    """Jobs of one repository run one at a time (`lock`), so database updates cannot race."""
    await queue.configure_task(name=task, lock=str(repository_id)).defer_async(
        repository_id=str(repository_id), **arguments
    )
