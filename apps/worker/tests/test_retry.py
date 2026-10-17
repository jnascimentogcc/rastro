import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit.exception import PrimaryRateLimitExceeded, RequestFailed, RequestTimeout
from githubkit.response import Response
from procrastinate import testing
from procrastinate.jobs import Job
from pydantic import BaseModel

from rastro_llm import LLMError, LLMTransientError
from rastro_worker import indexing, tasks
from rastro_worker.app import app
from rastro_worker.retry import TransientErrorsOnly, backoff_seconds, is_transient

REQUEST = httpx.Request("GET", "https://api.github.test/repos/acme/clinic")


def failed(status: int, headers: dict[str, str] | None = None) -> RequestFailed:
    raw = httpx.Response(status, request=REQUEST, headers=headers, json={"message": "nope"})
    return RequestFailed(Response(raw, BaseModel))


def job(attempts: int = 0) -> Job:
    return Job(
        id=7,
        queue="default",
        lock=None,
        queueing_lock=None,
        task_name="analyze_pr",
        task_kwargs={},
        attempts=attempts,
    )


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_github_server_errors_and_rate_limits_are_transient(status: int) -> None:
    assert is_transient(failed(status))


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_github_client_errors_are_permanent(status: int) -> None:
    assert not is_transient(failed(status))  # e.g. 404: the installation is gone


def test_timeouts_rate_limits_and_network_errors_are_transient() -> None:
    assert is_transient(RequestTimeout(httpx.ReadTimeout("slow", request=REQUEST)))
    assert is_transient(httpx.ConnectError("down"))
    assert is_transient(LLMTransientError("429 from the provider"))
    raw = httpx.Response(403, request=REQUEST, json={})
    limited = PrimaryRateLimitExceeded(Response(raw, BaseModel), timedelta(seconds=90))
    assert is_transient(limited)


def test_model_refusals_and_programming_errors_are_permanent() -> None:
    assert not is_transient(LLMError("model refused"))
    assert not is_transient(ValueError("bad payload"))
    assert not is_transient(KeyError("x"))


def test_backoff_grows_exponentially_and_is_capped() -> None:
    error = failed(503)
    delays = [backoff_seconds(n, error) for n in range(8)]
    assert delays[:4] == [30, 60, 120, 240]
    assert delays == sorted(delays) and delays[-1] == 30 * 60


def test_retry_after_of_a_rate_limit_is_honoured() -> None:
    raw = httpx.Response(403, request=REQUEST, json={})
    limited = PrimaryRateLimitExceeded(Response(raw, BaseModel), timedelta(minutes=20))
    assert backoff_seconds(0, limited) == 20 * 60 + 1


def test_strategy_retries_transient_gives_up_after_the_limit_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = TransientErrorsOnly(max_retries=2)
    with caplog.at_level(logging.WARNING):
        assert strategy.get_retry_decision(exception=failed(503), job=job(0)) is not None
        assert strategy.get_retry_decision(exception=failed(503), job=job(1)) is not None
        assert strategy.get_retry_decision(exception=failed(503), job=job(2)) is None
        assert strategy.get_retry_decision(exception=failed(404), job=job(0)) is None
    text = caplog.text
    assert "retry 1/2" in text and "giving up" in text and "failed permanently" in text


@pytest.mark.parametrize(
    "task",
    ["index_repository", "analyze_commit", "rule_extraction", "resync_anchors", "analyze_pr"],
)
def test_every_pipeline_job_uses_the_retry_strategy(task: str) -> None:
    assert isinstance(app.tasks[task].retry_strategy, TransientErrorsOnly)


async def _run_index_job(monkeypatch: pytest.MonkeyPatch, error: Exception) -> dict[str, Any]:
    async def boom(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(indexing, "index_repository", boom)
    monkeypatch.setattr(tasks, "get_deps", lambda: SimpleNamespace())

    async def active(repository_id: str) -> bool:
        return True

    monkeypatch.setattr(tasks, "repository_is_active", active)
    with app.replace_connector(testing.InMemoryConnector()) as test_app:
        async with test_app.open_async():
            await tasks.index_repository.defer_async(
                repository_id="00000000-0000-0000-0000-000000000001"
            )
            await test_app.run_worker_async(wait=False)
            return dict(next(iter(test_app.connector.jobs.values())))  # type: ignore[attr-defined]


async def test_transient_failure_is_rescheduled_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _run_index_job(monkeypatch, failed(503))

    assert result["status"] == "todo"  # waiting for its next attempt
    assert result["attempts"] == 1
    assert result["scheduled_at"] is not None


async def test_permanent_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _run_index_job(monkeypatch, failed(404))

    assert result["status"] == "failed"
    assert result["attempts"] == 1
