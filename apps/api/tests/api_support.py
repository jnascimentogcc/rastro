import uuid
from dataclasses import dataclass

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class ApiContext:
    client: AsyncClient
    token: str
    repository_id: uuid.UUID
    engine: AsyncEngine
