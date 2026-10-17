"""Retry policy for the jobs: transient failures are retried with exponential backoff, permanent
ones fail at once. Every decision is logged so a failing job is visible in the worker log.

Transient: GitHub 5xx, 429 and rate limits (honouring `Retry-After`), network errors and
timeouts, model rate limits/timeouts/5xx (`LLMTransientError`), lost database connections.
Permanent (never retried): GitHub 401/403/404/422 (for instance an uninstalled App), model
refusals and invalid answers, validation and programming errors.
"""

import logging
from datetime import timedelta

import httpx
from githubkit.exception import RateLimitExceeded, RequestError, RequestFailed, RequestTimeout
from procrastinate.jobs import Job
from procrastinate.retry import BaseRetryStrategy, RetryDecision
from sqlalchemy.exc import DBAPIError

from rastro_llm import LLMTransientError

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_DELAY_SECONDS = 30
MAX_DELAY_SECONDS = 30 * 60


def is_transient(error: BaseException) -> bool:
    if isinstance(error, LLMTransientError | RateLimitExceeded | RequestTimeout):
        return True
    if isinstance(error, RequestFailed):
        return error.response.status_code == 429 or error.response.status_code >= 500
    if isinstance(error, RequestError):  # transport failure wrapped by githubkit
        return isinstance(error.exc, httpx.TransportError)
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, DBAPIError):
        return bool(error.connection_invalidated)
    return False


def backoff_seconds(attempts: int, error: BaseException) -> int:
    """30 s, 1 min, 2 min, 4 min, 8 min... capped; a GitHub `Retry-After` wins if longer."""
    delay = min(BASE_DELAY_SECONDS * 2**attempts, MAX_DELAY_SECONDS)
    if isinstance(error, RateLimitExceeded):
        retry_after: timedelta = error.retry_after
        delay = max(delay, min(int(retry_after.total_seconds()) + 1, 60 * 60))
    return int(delay)


class TransientErrorsOnly(BaseRetryStrategy):
    def __init__(self, max_retries: int = MAX_RETRIES) -> None:
        self.max_retries = max_retries

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        name = f"{job.task_name}[job {job.id}]"
        if not is_transient(exception):
            logger.error("%s failed permanently, not retrying: %r", name, exception)
            return None
        if job.attempts >= self.max_retries:
            logger.error(
                "%s still failing after %d retries, giving up: %r", name, job.attempts, exception
            )
            return None
        delay = backoff_seconds(job.attempts, exception)
        logger.warning(
            "%s failed with a transient error, retry %d/%d in %ds: %r",
            name,
            job.attempts + 1,
            self.max_retries,
            delay,
            exception,
        )
        return RetryDecision(retry_in={"seconds": delay})


retry_transient = TransientErrorsOnly()
