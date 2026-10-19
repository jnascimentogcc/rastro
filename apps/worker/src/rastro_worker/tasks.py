import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps

from githubkit.exception import GitHubException, RequestFailed
from sqlalchemy import select

from rastro_db.models import Repository
from rastro_worker import impact, indexing, merged_pr, resync, review, rule_extraction
from rastro_worker.app import app
from rastro_worker.deps import get_deps
from rastro_worker.retry import retry_transient

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


async def repository_is_active(repository_id: str) -> bool:
    """Jobs of a repository whose App installation was removed or suspended are dropped (the
    data stays, see the README). Queued jobs outlive the webhook that deactivated it."""
    deps = get_deps()
    async with deps.session_factory() as session:
        status = await session.scalar(
            select(Repository.status).where(Repository.id == uuid.UUID(repository_id))
        )
    if status is not None and status != "active":
        logger.warning("skipping job for repository %s: it is %s", repository_id, status)
        return False
    return True  # unknown repositories are left to the job itself to report


@app.task(name="ping")
async def ping(message: str = "pong") -> str:
    """Smoke-test task proving the queue and the worker are wired up."""
    return message


@app.task(name="index_repository", retry=retry_transient)
@log_github_errors
async def index_repository(repository_id: str) -> None:
    if not await repository_is_active(repository_id):
        return
    await indexing.index_repository(get_deps(), uuid.UUID(repository_id))
    await resync_anchors.configure(lock=repository_id).defer_async(repository_id=repository_id)


@app.task(name="analyze_commit", retry=retry_transient)
@log_github_errors
async def analyze_commit(repository_id: str, sha: str, on_default_branch: bool = False) -> None:
    if not await repository_is_active(repository_id):
        return
    summary = await indexing.analyze_commit(
        get_deps(), uuid.UUID(repository_id), sha, on_default_branch
    )
    if on_default_branch:
        await resync_anchors.configure(lock=repository_id).defer_async(repository_id=repository_id)
    if on_default_branch and summary.get("sessions"):
        await extract_rules.configure(lock=repository_id).defer_async(
            repository_id=repository_id, sha=sha
        )


@app.task(name="rule_extraction", retry=retry_transient)
@log_github_errors
async def extract_rules(repository_id: str, sha: str, force: bool = False) -> None:
    """Rule extraction for one commit; `analyze_commit` and late session uploads queue it."""
    if not await repository_is_active(repository_id):
        return
    await rule_extraction.extract_rules(get_deps(), uuid.UUID(repository_id), sha, force=force)


@app.task(name="resync_anchors", retry=retry_transient)
async def resync_anchors(repository_id: str) -> None:
    """Follow renamed/moved code with the rule anchors; orphan rules whose code is gone."""
    if not await repository_is_active(repository_id):
        return
    await resync.resync_anchors(get_deps(), uuid.UUID(repository_id))


@app.task(name="analyze_pr", retry=retry_transient)
@log_github_errors
async def analyze_pr(repository_id: str, number: int, head_sha: str) -> None:
    """Impact analysis of a pull request; the single PR comment is edited in place."""
    if not await repository_is_active(repository_id):
        return
    await impact.analyze_pull_request(get_deps(), uuid.UUID(repository_id), number, head_sha)


@app.task(name="pull_request_merged", retry=retry_transient)
@log_github_errors
async def pull_request_merged(
    repository_id: str, number: int, merge_commit_sha: str | None = None
) -> None:
    """A pull request was merged (merge, squash or rebase). Release the sessions of its own
    commits for rule extraction, oldest commit first; jobs of a repository run in the order they
    were queued, so later commits see (and may update) the rules the earlier ones wrote.
    The merge/squash commit itself is analysed first so symbols exist for the merged state."""
    if not await repository_is_active(repository_id):
        return
    merged = await merged_pr.release_pull_request_commits(
        get_deps(), uuid.UUID(repository_id), number
    )
    if merge_commit_sha:
        await analyze_commit.configure(lock=repository_id).defer_async(
            repository_id=repository_id, sha=merge_commit_sha, on_default_branch=True
        )
    for sha in merged.extract_shas:
        await extract_rules.configure(lock=repository_id).defer_async(
            repository_id=repository_id, sha=sha
        )


@app.task(name="apply_review", retry=retry_transient)
@log_github_errors
async def apply_review(
    repository_id: str,
    number: int,
    comment_id: int,
    sender: str,
    event_action: str,
    body: str,
    previous_body: str = "",
) -> None:
    """A person ticked a box in Rastro's comment or wrote a `/rastro ...` command on a PR."""
    if not await repository_is_active(repository_id):
        return
    await review.apply_review(
        get_deps(),
        uuid.UUID(repository_id),
        number,
        comment_id=comment_id,
        sender=sender,
        event_action=event_action,
        body=body,
        previous_body=previous_body,
    )
