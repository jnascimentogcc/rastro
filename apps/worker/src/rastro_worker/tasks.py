import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec

from githubkit.exception import GitHubException, RequestFailed

from rastro_worker import indexing
from rastro_worker.app import app
from rastro_worker.deps import get_deps

logger = logging.getLogger(__name__)
P = ParamSpec("P")


def log_github_errors[**P](task: Callable[P, Awaitable[None]]) -> Callable[P, Awaitable[None]]:
    """Log what GitHub answered (status, URL, message) before the job fails, so a bad App
    configuration shows up in the worker log instead of a bare 404."""

    @wraps(task)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await task(*args, **kwargs)
        except GitHubException as error:
            # githubkit wraps the failed response (e.g. during token exchange) in RequestError
            failed = error if isinstance(error, RequestFailed) else error.__cause__
            if isinstance(failed, RequestFailed):
                response = failed.response
                logger.error(
                    "GitHub %s %s -> %s: %s",
                    response.raw_request.method,
                    response.raw_request.url,
                    response.status_code,
                    response.text[:500],
                )
            raise

    return wrapper


@app.task(name="ping")
async def ping(message: str = "pong") -> str:
    """Smoke-test task proving the queue and the worker are wired up."""
    return message


@app.task(name="index_repository")
@log_github_errors
async def index_repository(repository_id: str) -> None:
    await indexing.index_repository(get_deps(), uuid.UUID(repository_id))


@app.task(name="analyze_commit")
@log_github_errors
async def analyze_commit(repository_id: str, sha: str, on_default_branch: bool = False) -> None:
    await indexing.analyze_commit(get_deps(), uuid.UUID(repository_id), sha, on_default_branch)
