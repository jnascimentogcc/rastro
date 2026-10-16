import json
from typing import Any

import pytest
from api_support import WEBHOOK_SECRET, ApiContext
from httpx import Response
from sqlalchemy import select

from rastro_db import create_session_factory
from rastro_db.models import Commit, Repository
from rastro_github import sign

pytestmark = pytest.mark.db

# Ids in this range are cleaned up by the `api` fixture.
GITHUB_ID = 9_100_001
OTHER_GITHUB_ID = 9_100_002
SHA_A, SHA_B = "a" * 40, "b" * 40


async def deliver(
    api: ApiContext,
    event: str,
    payload: dict[str, Any],
    *,
    secret: str | None = WEBHOOK_SECRET,
    body: bytes | None = None,
) -> Response:
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if secret is not None:
        headers["X-Hub-Signature-256"] = sign(secret, raw)
    return await api.client.post("/webhooks/github", content=raw, headers=headers)


def push_payload(ref: str = "refs/heads/main", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ref": ref,
        "deleted": False,
        "installation": {"id": 4242},
        "repository": {
            "id": GITHUB_ID,
            "full_name": "acme/webhook-clinic",
            "default_branch": "main",
        },
        "commits": [
            {
                "id": SHA_A,
                "message": "first",
                "timestamp": "2026-10-01T10:00:00+00:00",
                "author": {"name": "Dev"},
            },
            {
                "id": SHA_B,
                "message": "second",
                "timestamp": "2026-10-01T10:05:00+00:00",
                "author": {"name": "Dev"},
            },
        ],
        "head_commit": {"id": SHA_B, "message": "second", "timestamp": "2026-10-01T10:05:00+00:00"},
    }
    return {**payload, **overrides}


async def stored_commits(api: ApiContext) -> list[Commit]:
    async with create_session_factory(api.engine)() as session:
        repo = await session.scalar(select(Repository).where(Repository.github_id == GITHUB_ID))
        if repo is None:
            return []
        return list(await session.scalars(select(Commit).where(Commit.repository_id == repo.id)))


# --------------------------------------------------------------------------- security


async def test_invalid_signature_is_rejected_and_nothing_is_stored(api: ApiContext) -> None:
    response = await deliver(api, "push", push_payload(), secret="wrong-secret")

    assert response.status_code == 401
    assert await stored_commits(api) == []
    assert api.queue.jobs == {}


async def test_missing_signature_is_rejected(api: ApiContext) -> None:
    assert (await deliver(api, "push", push_payload(), secret=None)).status_code == 401


async def test_signature_is_checked_over_the_raw_body(api: ApiContext) -> None:
    raw = json.dumps(push_payload()).encode()
    headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": sign(WEBHOOK_SECRET, raw)}

    tampered = await api.client.post("/webhooks/github", content=raw + b" ", headers=headers)

    assert tampered.status_code == 401


async def test_unconfigured_secret_refuses_all_deliveries(api: ApiContext) -> None:
    from pydantic import SecretStr  # noqa: F401

    api.client._transport.app.state.github_settings.github_webhook_secret = None  # type: ignore[attr-defined]

    assert (await deliver(api, "ping", {})).status_code == 503


async def test_signed_non_json_body_is_a_bad_request(api: ApiContext) -> None:
    assert (await deliver(api, "push", {}, body=b"not json")).status_code == 400


# --------------------------------------------------------------------------- events


async def test_ping_and_unknown_events_are_accepted(api: ApiContext) -> None:
    ping = await deliver(api, "ping", {"zen": "Design for failure."})
    other = await deliver(api, "star", {"action": "created"})

    assert ping.status_code == 202 and ping.json()["handled"] is True
    assert other.status_code == 202 and other.json()["handled"] is False
    assert api.queue.jobs == {}


async def test_installation_registers_repositories_and_queues_initial_index(
    api: ApiContext,
) -> None:
    payload = {
        "action": "created",
        "installation": {"id": 4242},
        "repositories": [
            {"id": GITHUB_ID, "full_name": "acme/one"},
            {"id": OTHER_GITHUB_ID, "full_name": "acme/two"},
        ],
    }

    response = await deliver(api, "installation", payload)

    assert response.status_code == 202 and response.json()["handled"] is True
    async with create_session_factory(api.engine)() as session:
        repos = {
            r.full_name: r
            for r in await session.scalars(
                select(Repository).where(Repository.github_id.in_([GITHUB_ID, OTHER_GITHUB_ID]))
            )
        }
    assert set(repos) == {"acme/one", "acme/two"}
    assert all(r.installation_id == 4242 for r in repos.values())
    jobs = api.jobs("index_repository")
    assert {j["args"]["repository_id"] for j in jobs} == {str(r.id) for r in repos.values()}  # type: ignore[index]
    assert all(j["lock"] == j["args"]["repository_id"] for j in jobs)  # type: ignore[index]


async def test_repositories_added_to_an_existing_installation_are_indexed(api: ApiContext) -> None:
    payload = {
        "action": "added",
        "installation": {"id": 4242},
        "repositories_added": [{"id": GITHUB_ID, "full_name": "acme/new"}],
    }

    await deliver(api, "installation_repositories", payload)

    assert len(api.jobs("index_repository")) == 1


async def test_installation_deleted_is_not_acted_upon_yet(api: ApiContext) -> None:
    payload = {"action": "deleted", "installation": {"id": 4242}, "repositories": []}

    response = await deliver(api, "installation", payload)

    assert response.json()["handled"] is False
    assert api.queue.jobs == {}


async def test_push_to_default_branch_stores_commits_and_queues_analysis(api: ApiContext) -> None:
    response = await deliver(api, "push", push_payload())

    assert response.status_code == 202 and response.json()["handled"] is True
    commits = await stored_commits(api)
    assert {c.sha: c.message for c in commits} == {SHA_A: "first", SHA_B: "second"}
    assert commits[0].author == "Dev"
    jobs = api.jobs("analyze_commit")
    assert {j["args"]["sha"] for j in jobs} == {SHA_A, SHA_B}  # type: ignore[index]
    assert all(j["args"]["on_default_branch"] is True for j in jobs)  # type: ignore[index]


async def test_push_to_feature_branch_is_marked_as_not_default(api: ApiContext) -> None:
    await deliver(api, "push", push_payload(ref="refs/heads/feature/x"))

    jobs = api.jobs("analyze_commit")
    assert len(jobs) == 2
    assert all(j["args"]["on_default_branch"] is False for j in jobs)  # type: ignore[index]


async def test_redelivery_is_idempotent_for_commits(api: ApiContext) -> None:
    await deliver(api, "push", push_payload())
    await deliver(api, "push", push_payload())

    assert len(await stored_commits(api)) == 2


async def test_push_without_listed_commits_uses_the_head_commit(api: ApiContext) -> None:
    await deliver(api, "push", push_payload(commits=[]))

    assert [c.sha for c in await stored_commits(api)] == [SHA_B]


@pytest.mark.parametrize(
    "overrides",
    [{"ref": "refs/tags/v1"}, {"deleted": True}, {"commits": [], "head_commit": None}],
)
async def test_pushes_without_commits_to_analyze_are_ignored(
    api: ApiContext, overrides: dict[str, Any]
) -> None:
    response = await deliver(api, "push", push_payload(**overrides))

    assert response.json()["handled"] is False
    assert api.queue.jobs == {}


async def test_pull_request_events_are_accepted_but_not_handled_yet(api: ApiContext) -> None:
    response = await deliver(api, "pull_request", {"action": "opened"})

    assert response.status_code == 202 and response.json()["handled"] is False
