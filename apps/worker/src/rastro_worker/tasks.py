import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps

from githubkit.exception import GitHubException, RequestFailed

from rastro_worker import indexing, resync, rule_extraction
from rastro_worker.app import app
from rastro_worker.deps import get_deps

logger = logging.getLogger(__name__)


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
    await resync_anchors.configure(lock=repository_id).defer_async(repository_id=repository_id)


@app.task(name="analyze_commit")
@log_github_errors
async def analyze_commit(repository_id: str, sha: str, on_default_branch: bool = False) -> None:
    summary = await indexing.analyze_commit(
        get_deps(), uuid.UUID(repository_id), sha, on_default_branch
    )
    if on_default_branch:
        await resync_anchors.configure(lock=repository_id).defer_async(repository_id=repository_id)
    if summary.get("sessions"):
        await extract_rules.configure(lock=repository_id).defer_async(
            repository_id=repository_id, sha=sha
        )


@app.task(name="rule_extraction")
@log_github_errors
async def extract_rules(repository_id: str, sha: str, force: bool = False) -> None:
    """Rule extraction for one commit; `analyze_commit` and late session uploads queue it."""
    await rule_extraction.extract_rules(get_deps(), uuid.UUID(repository_id), sha, force=force)


@app.task(name="resync_anchors")
async def resync_anchors(repository_id: str) -> None:
    """Follow renamed/moved code with the rule anchors; orphan rules whose code is gone."""
    await resync.resync_anchors(get_deps(), uuid.UUID(repository_id))
