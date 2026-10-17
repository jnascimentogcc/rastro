from typing import Any

import pytest
from api_support import ApiContext
from sqlalchemy import select, text, update

from rastro_db import create_session_factory
from rastro_db.models import AgentSession, Repository

pytestmark = pytest.mark.db

PAYLOAD: dict[str, Any] = {
    "commit_sha": "A" * 40,
    "session_ref": "sess-1",
    "agent": "claude-code",
    "agent_version": "2.1.288",
    "git_branch": "main",
    "excerpts": [
        {
            "role": "user",
            "kind": "intent",
            "text": "Implants cannot be booked on Friday afternoon",
            "timestamp": "2026-10-01T10:00:00.000Z",
            "files": ["src/agenda.ts"],
        }
    ],
}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_whoami_with_valid_token(api: ApiContext) -> None:
    response = await api.client.get("/v1/auth/whoami", headers=auth(api.token))

    assert response.status_code == 200
    assert response.json()["full_name"] == "acme/clinic"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer rst_wrong"}])
async def test_missing_or_wrong_token_is_rejected(api: ApiContext, headers: dict[str, str]) -> None:
    for method, url in (("GET", "/v1/auth/whoami"), ("POST", "/v1/sessions")):
        response = await api.client.request(method, url, headers=headers, json=PAYLOAD)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


async def test_capture_is_stored_and_sha_is_lowercased(api: ApiContext) -> None:
    response = await api.client.post("/v1/sessions", json=PAYLOAD, headers=auth(api.token))

    assert response.status_code == 201
    assert response.json()["created"] is True
    factory = create_session_factory(api.engine)
    async with factory() as session:
        stored = (
            await session.scalars(
                select(AgentSession).where(AgentSession.repository_id == api.repository_id)
            )
        ).one()
    assert stored.commit_sha == "a" * 40
    assert stored.agent_version == "2.1.288"
    assert stored.excerpts[0]["kind"] == "intent"


async def test_resending_the_same_capture_replaces_it(api: ApiContext) -> None:
    first = await api.client.post("/v1/sessions", json=PAYLOAD, headers=auth(api.token))
    changed = {
        **PAYLOAD,
        "excerpts": [*PAYLOAD["excerpts"], {**PAYLOAD["excerpts"][0], "kind": "context"}],
    }
    second = await api.client.post("/v1/sessions", json=changed, headers=auth(api.token))

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created"] is False
    assert second.json()["excerpts"] == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"commit_sha": "not-a-sha"},
        {"session_ref": ""},
        {"excerpts": [{"role": "system", "kind": "intent", "text": "x", "timestamp": "t"}]},
        {"excerpts": [{"role": "user", "kind": "intent", "text": "x" * 150_000, "timestamp": "t"}]},
        {
            "excerpts": [
                {"role": "user", "kind": "context", "text": "x" * 19_000, "timestamp": "t"}
            ]
            * 11
        },
    ],
)
async def test_invalid_payloads_are_rejected(api: ApiContext, patch: dict[str, object]) -> None:
    response = await api.client.post(
        "/v1/sessions", json={**PAYLOAD, **patch}, headers=auth(api.token)
    )

    assert response.status_code == 422


async def test_upload_queues_extraction_only_when_the_commit_is_already_known(
    api: ApiContext,
) -> None:
    from rastro_db.models import Commit

    first = await api.client.post("/v1/sessions", json=PAYLOAD, headers=auth(api.token))
    assert first.status_code == 201
    assert api.jobs("rule_extraction") == []  # the push webhook has not arrived yet

    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(Commit(repository_id=api.repository_id, sha="a" * 40))
    late = {**PAYLOAD, "session_ref": "sess-2"}
    await api.client.post("/v1/sessions", json=late, headers=auth(api.token))

    [job] = api.jobs("rule_extraction")
    assert job["args"] == {"repository_id": str(api.repository_id), "sha": "a" * 40}
    assert job["lock"] == str(api.repository_id)


async def test_capture_is_refused_while_the_app_is_not_installed(api: ApiContext) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        await session.execute(
            update(Repository)
            .where(Repository.id == api.repository_id)
            .values(status="uninstalled")
        )

    response = await api.client.post("/v1/sessions", json=PAYLOAD, headers=auth(api.token))

    assert response.status_code == 403 and "uninstalled" in response.json()["detail"]


async def test_excerpts_are_encrypted_in_the_database(api: ApiContext) -> None:
    await api.client.post("/v1/sessions", json=PAYLOAD, headers=auth(api.token))

    async with create_session_factory(api.engine)() as session:
        raw = await session.scalar(
            text("SELECT excerpts::text FROM agent_sessions WHERE repository_id = :r"),
            {"r": api.repository_id},
        )
        stored = await session.scalar(
            select(AgentSession).where(AgentSession.repository_id == api.repository_id)
        )

    assert raw is not None and "Implants" not in raw and '"ct"' in raw
    assert stored is not None and stored.excerpts[0]["text"].startswith("Implants")


@pytest.mark.parametrize(("flag", "queued"), [(True, 1), (None, 1), (False, 0)])
async def test_a_late_session_queues_extraction_only_when_the_commit_may_be_analysed(
    api: ApiContext, flag: bool | None, queued: int
) -> None:
    from rastro_db.models import Commit

    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(Commit(repository_id=api.repository_id, sha="a" * 40, on_default_branch=flag))

    await api.client.post("/v1/sessions", json=PAYLOAD, headers=auth(api.token))

    assert len(api.jobs("rule_extraction")) == queued  # a feature-branch commit waits for the merge
