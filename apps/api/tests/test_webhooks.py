import json
import uuid
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
RUN = f"-{uuid.uuid4()}"  # delivery ids persist, so every test run needs fresh ones


async def deliver(
    api: ApiContext,
    event: str,
    payload: dict[str, Any],
    *,
    secret: str | None = WEBHOOK_SECRET,
    body: bytes | None = None,
    delivery_id: str | None = None,
) -> Response:
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
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


async def install(api: ApiContext) -> None:
    payload = {
        "action": "created",
        "installation": {"id": 4242},
        "repositories": [
            {"id": GITHUB_ID, "full_name": "acme/one"},
            {"id": OTHER_GITHUB_ID, "full_name": "acme/two"},
        ],
    }
    await deliver(api, "installation", payload)
    api.queue.jobs.clear()


async def statuses(api: ApiContext) -> dict[int, str]:
    async with create_session_factory(api.engine)() as session:
        rows = await session.execute(
            select(Repository.github_id, Repository.status).where(
                Repository.github_id.in_([GITHUB_ID, OTHER_GITHUB_ID])
            )
        )
    return {github_id: status for github_id, status in rows}


async def test_installation_deleted_marks_repositories_uninstalled_and_keeps_them(
    api: ApiContext,
) -> None:
    await install(api)
    payload = {"action": "deleted", "installation": {"id": 4242}, "repositories": []}

    response = await deliver(api, "installation", payload)

    assert response.json()["handled"] is True
    assert await statuses(api) == {GITHUB_ID: "uninstalled", OTHER_GITHUB_ID: "uninstalled"}
    assert api.queue.jobs == {}  # nothing is analysed any more


async def test_suspend_pauses_and_unsuspend_resumes_with_a_fresh_index(api: ApiContext) -> None:
    await install(api)

    await deliver(api, "installation", {"action": "suspend", "installation": {"id": 4242}})
    assert set((await statuses(api)).values()) == {"suspended"}

    await deliver(api, "installation", {"action": "unsuspend", "installation": {"id": 4242}})

    assert set((await statuses(api)).values()) == {"active"}
    assert len(api.jobs("index_repository")) == 2  # pushes during the suspension were missed


async def test_removing_a_repository_from_the_installation_only_affects_that_repository(
    api: ApiContext,
) -> None:
    await install(api)
    payload = {
        "action": "removed",
        "installation": {"id": 4242},
        "repositories_removed": [{"id": OTHER_GITHUB_ID, "full_name": "acme/two"}],
    }

    await deliver(api, "installation_repositories", payload)

    assert await statuses(api) == {GITHUB_ID: "active", OTHER_GITHUB_ID: "uninstalled"}


async def test_reinstalling_reactivates_the_repositories(api: ApiContext) -> None:
    await install(api)
    await deliver(api, "installation", {"action": "deleted", "installation": {"id": 4242}})

    await install(api)

    assert set((await statuses(api)).values()) == {"active"}


async def test_events_of_inactive_repositories_are_ignored(api: ApiContext) -> None:
    await install(api)
    await deliver(api, "installation", {"action": "suspend", "installation": {"id": 4242}})

    pushed = await deliver(api, "push", push_payload())
    opened = await deliver(api, "pull_request", pull_request_payload())

    assert pushed.json()["handled"] is False and opened.json()["handled"] is False
    assert await stored_commits(api) == []
    assert api.queue.jobs == {}


async def test_a_replayed_delivery_is_ignored(api: ApiContext) -> None:
    first = await deliver(api, "push", push_payload(), delivery_id="delivery-1" + RUN)
    api.queue.jobs.clear()

    replay = await deliver(api, "push", push_payload(), delivery_id="delivery-1" + RUN)

    assert first.json()["handled"] is True
    assert replay.status_code == 202 and replay.json()["duplicate"] is True
    assert api.queue.jobs == {}


async def test_different_deliveries_with_the_same_content_are_both_processed(
    api: ApiContext,
) -> None:
    await deliver(api, "push", push_payload(), delivery_id="delivery-a" + RUN)
    second = await deliver(api, "push", push_payload(), delivery_id="delivery-b" + RUN)

    assert second.json().get("duplicate") is None and second.json()["handled"] is True


async def test_a_failed_delivery_is_not_remembered_so_github_can_redeliver_it(
    api: ApiContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rastro_api.routers import github as router

    async def boom(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("database hiccup")

    monkeypatch.setitem(router._HANDLERS, "push", boom)
    with pytest.raises(RuntimeError):
        await deliver(api, "push", push_payload(), delivery_id="delivery-x" + RUN)
    monkeypatch.undo()

    retry = await deliver(api, "push", push_payload(), delivery_id="delivery-x" + RUN)

    assert retry.json()["handled"] is True


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


def pull_request_payload(action: str = "opened", sha: str = SHA_A) -> dict[str, Any]:
    return {
        "action": action,
        "number": 7,
        "installation": {"id": 4242},
        "repository": {
            "id": GITHUB_ID,
            "full_name": "acme/webhook-clinic",
            "default_branch": "main",
        },
        "pull_request": {"number": 7, "head": {"sha": sha}, "draft": False},
    }


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
async def test_pull_request_activity_queues_an_impact_analysis(
    api: ApiContext, action: str
) -> None:
    response = await deliver(api, "pull_request", pull_request_payload(action))

    assert response.status_code == 202 and response.json()["handled"] is True
    [job] = api.jobs("analyze_pr")
    assert job["args"]["number"] == 7 and job["args"]["head_sha"] == SHA_A  # type: ignore[index]
    assert job["lock"] == job["args"]["repository_id"]  # type: ignore[index]
    async with create_session_factory(api.engine)() as session:
        repo = await session.scalar(select(Repository).where(Repository.github_id == GITHUB_ID))
    assert repo is not None and repo.installation_id == 4242


async def test_a_waiting_analysis_absorbs_further_pushes_to_the_same_pull_request(
    api: ApiContext,
) -> None:
    await deliver(api, "pull_request", pull_request_payload("opened", SHA_A))
    await deliver(api, "pull_request", pull_request_payload("synchronize", SHA_B))

    assert len(api.jobs("analyze_pr")) == 1  # it reads the latest state when it runs


@pytest.mark.parametrize("action", ["labeled", "edited", "assigned"])
async def test_other_pull_request_actions_are_ignored(api: ApiContext, action: str) -> None:
    response = await deliver(api, "pull_request", pull_request_payload(action))

    assert response.status_code == 202 and response.json()["handled"] is False
    assert api.queue.jobs == {}


async def test_a_merged_pull_request_queues_the_release_of_its_commits(api: ApiContext) -> None:
    payload = pull_request_payload("closed")
    payload["pull_request"].update(merged=True, merge_commit_sha=SHA_B)

    response = await deliver(api, "pull_request", payload, delivery_id="merged-1" + RUN)

    assert response.status_code == 202 and response.json()["handled"] is True
    [job] = api.jobs("pull_request_merged")
    assert job["args"]["number"] == 7 and job["args"]["merge_commit_sha"] == SHA_B  # type: ignore[index]
    assert job["lock"] == job["args"]["repository_id"]  # type: ignore[index]
    assert api.jobs("analyze_pr") == []


async def test_a_redelivered_merge_queues_nothing_more(api: ApiContext) -> None:
    payload = pull_request_payload("closed")
    payload["pull_request"].update(merged=True, merge_commit_sha=SHA_B)

    await deliver(api, "pull_request", payload, delivery_id="merged-2" + RUN)
    replay = await deliver(api, "pull_request", payload, delivery_id="merged-2" + RUN)
    again = await deliver(api, "pull_request", payload, delivery_id="merged-3" + RUN)

    assert replay.json()["duplicate"] is True
    assert again.json()["handled"] is True  # a new delivery while the job still waits: absorbed
    assert len(api.jobs("pull_request_merged")) == 1


@pytest.mark.parametrize("merged", [False, None])
async def test_a_pull_request_closed_without_merging_does_nothing(
    api: ApiContext, merged: bool | None
) -> None:
    payload = pull_request_payload("closed")
    if merged is not None:
        payload["pull_request"]["merged"] = merged

    response = await deliver(api, "pull_request", payload)

    assert response.status_code == 202 and response.json()["handled"] is False
    assert api.queue.jobs == {}


async def test_the_push_records_whether_the_commits_are_on_the_default_branch(
    api: ApiContext,
) -> None:
    await deliver(api, "push", push_payload(ref="refs/heads/feature/x"))
    assert {c.on_default_branch for c in await stored_commits(api)} == {False}

    await deliver(api, "push", push_payload(ref="refs/heads/main", commits=[]))  # only the head

    flags = {c.sha: c.on_default_branch for c in await stored_commits(api)}
    assert flags == {SHA_A: False, SHA_B: True}


async def test_merging_a_pull_request_flips_all_its_commits_and_the_flag_never_goes_back(
    api: ApiContext,
) -> None:
    await deliver(api, "push", push_payload(ref="refs/heads/feature/x"))

    await deliver(api, "push", push_payload(ref="refs/heads/main"))  # the merge pushes both again
    assert {c.on_default_branch for c in await stored_commits(api)} == {True}

    await deliver(api, "push", push_payload(ref="refs/heads/feature/x"))  # a late redelivery
    assert {c.on_default_branch for c in await stored_commits(api)} == {True}
