import uuid

from rastro_worker import indexing
from rastro_worker.app import app
from rastro_worker.deps import get_deps


@app.task(name="ping")
async def ping(message: str = "pong") -> str:
    """Smoke-test task proving the queue and the worker are wired up."""
    return message


@app.task(name="index_repository")
async def index_repository(repository_id: str) -> None:
    await indexing.index_repository(get_deps(), uuid.UUID(repository_id))


@app.task(name="analyze_commit")
async def analyze_commit(repository_id: str, sha: str, on_default_branch: bool = False) -> None:
    await indexing.analyze_commit(get_deps(), uuid.UUID(repository_id), sha, on_default_branch)
