import uuid
from dataclasses import dataclass

from httpx import AsyncClient
from procrastinate import testing
from sqlalchemy.ext.asyncio import AsyncEngine

WEBHOOK_SECRET = "test-webhook-secret"


@dataclass
class ApiContext:
    client: AsyncClient
    token: str
    repository_id: uuid.UUID
    engine: AsyncEngine
    queue: testing.InMemoryConnector

    def jobs(self, task: str) -> list[dict[str, object]]:
        return [j for j in self.queue.jobs.values() if j["task_name"] == task]
