"""GitHub App webhook receiver.

Verifies the signature over the raw body, records what the event says (repositories, commits)
and enqueues the heavy work. It answers 202 immediately and never analyzes inside the request.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

import procrastinate
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_api.deps import DbSession
from rastro_api.queueing import enqueue
from rastro_db.models import Commit, Repository
from rastro_github import GitHubSettings, verify_signature

logger = logging.getLogger(__name__)
router = APIRouter(tags=["github"])


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request, session: DbSession) -> dict[str, Any]:
    settings: GitHubSettings = request.app.state.github_settings
    try:
        secret = settings.webhook_secret()
    except RuntimeError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook secret not configured"
        ) from None

    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not valid JSON") from None

    event = request.headers.get("x-github-event", "")
    action = payload.get("action") if isinstance(payload, dict) else None
    handler = _HANDLERS.get(event)
    if handler is None:
        return {"event": event, "action": action, "handled": False}
    handled = await handler(payload, session, request.app.state.queue)
    return {"event": event, "action": action, "handled": handled}


# --------------------------------------------------------------------------- handlers


async def _upsert_repository(
    session: AsyncSession,
    *,
    github_id: int,
    full_name: str,
    installation_id: int,
    default_branch: str | None = None,
) -> uuid.UUID:
    values = {
        "github_id": github_id,
        "full_name": full_name,
        "installation_id": installation_id,
        "default_branch": default_branch or "main",  # refreshed by index_repository
    }
    statement = insert(Repository).values(**values)
    updates = {"full_name": statement.excluded.full_name, "installation_id": installation_id}
    if default_branch:
        updates["default_branch"] = statement.excluded.default_branch
    result = await session.execute(
        statement.on_conflict_do_update(index_elements=["github_id"], set_=updates).returning(
            Repository.id
        )
    )
    return result.scalar_one()


async def _on_ping(
    payload: dict[str, Any], session: AsyncSession, queue: procrastinate.App
) -> bool:
    return True


async def _on_installation_repositories(
    payload: dict[str, Any], session: AsyncSession, queue: procrastinate.App
) -> bool:
    """`installation` (created) lists `repositories`; `installation_repositories` (added)
    lists `repositories_added`. Both register the repositories and start the initial index."""
    action = payload.get("action")
    if action not in ("created", "added"):
        return False  # deleted/removed/suspend...: kept for later phases
    installation_id = int(payload["installation"]["id"])
    listed = payload.get("repositories_added") or payload.get("repositories") or []
    repository_ids = [
        await _upsert_repository(
            session,
            github_id=int(item["id"]),
            full_name=item["full_name"],
            installation_id=installation_id,
        )
        for item in listed
    ]
    await session.commit()
    for repository_id in repository_ids:
        await enqueue(queue, "index_repository", repository_id)
    return True


async def _on_push(
    payload: dict[str, Any], session: AsyncSession, queue: procrastinate.App
) -> bool:
    ref = str(payload.get("ref", ""))
    if not ref.startswith("refs/heads/") or payload.get("deleted"):
        return False
    repo = payload["repository"]
    installation = payload.get("installation") or {}
    repository_id = await _upsert_repository(
        session,
        github_id=int(repo["id"]),
        full_name=repo["full_name"],
        installation_id=int(installation.get("id", 0))
        or await _known_installation(session, int(repo["id"])),
        default_branch=repo.get("default_branch"),
    )

    # `commits` holds at most 20 entries; the head commit is always covered.
    commits: dict[str, dict[str, Any]] = {c["id"]: c for c in payload.get("commits") or []}
    head = payload.get("head_commit")
    if head:
        commits.setdefault(head["id"], head)
    if not commits:
        return False

    for entry in commits.values():
        author = entry.get("author") or {}
        await session.execute(
            insert(Commit)
            .values(
                repository_id=repository_id,
                sha=entry["id"],
                author=author.get("name") or author.get("username"),
                message=entry.get("message"),
                committed_at=_parse_time(entry.get("timestamp")),
            )
            .on_conflict_do_nothing(index_elements=["repository_id", "sha"])
        )
    await session.commit()

    on_default_branch = ref == f"refs/heads/{repo.get('default_branch', 'main')}"
    for sha in commits:
        await enqueue(
            queue, "analyze_commit", repository_id, sha=sha, on_default_branch=on_default_branch
        )
    return True


async def _known_installation(session: AsyncSession, github_id: int) -> int:
    found = await session.scalar(
        select(Repository.installation_id).where(Repository.github_id == github_id)
    )
    return found or 0


_PULL_REQUEST_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


async def _on_pull_request(
    payload: dict[str, Any], session: AsyncSession, queue: procrastinate.App
) -> bool:
    if payload.get("action") not in _PULL_REQUEST_ACTIONS:
        return False
    repo = payload["repository"]
    pull_request = payload["pull_request"]
    installation = payload.get("installation") or {}
    repository_id = await _upsert_repository(
        session,
        github_id=int(repo["id"]),
        full_name=repo["full_name"],
        installation_id=int(installation.get("id", 0))
        or await _known_installation(session, int(repo["id"])),
        default_branch=repo.get("default_branch"),
    )
    await session.commit()
    number = int(payload["number"])
    # A job already waiting for this PR will read its latest state, so do not stack another.
    await enqueue(
        queue,
        "analyze_pr",
        repository_id,
        queueing_lock=f"pr:{repository_id}:{number}",
        number=number,
        head_sha=pull_request["head"]["sha"],
    )
    return True


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_HANDLERS = {
    "ping": _on_ping,
    "installation": _on_installation_repositories,
    "installation_repositories": _on_installation_repositories,
    "push": _on_push,
    "pull_request": _on_pull_request,
}
