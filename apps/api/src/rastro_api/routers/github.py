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
from sqlalchemy import false, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_api.deps import DbSession
from rastro_api.queueing import enqueue
from rastro_db.models import Commit, Repository, WebhookDelivery
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
    delivery_id = request.headers.get("x-github-delivery")
    if delivery_id and not await _first_delivery(session, delivery_id, event):
        logger.info("webhook %s (%s) already processed, ignoring the replay", delivery_id, event)
        return {"event": event, "action": action, "handled": False, "duplicate": True}
    handler = _HANDLERS.get(event)
    handled = False
    if handler is not None:
        handled = await handler(payload, session, request.app.state.queue)
    await session.commit()  # also records the delivery when the handler committed nothing
    return {"event": event, "action": action, "handled": handled}


async def _first_delivery(session: AsyncSession, delivery_id: str, event: str) -> bool:
    """Record the delivery id; False when it was seen before. The row is committed together
    with the handler's changes (or rolled back with them if the handler fails, so GitHub's
    redelivery of that same id is processed). A concurrent replay waits on the unique key."""
    inserted = await session.scalar(
        insert(WebhookDelivery)
        .values(delivery_id=delivery_id, event=event)
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        .returning(WebhookDelivery.delivery_id)
    )
    return inserted is not None


# --------------------------------------------------------------------------- handlers


async def _upsert_repository(
    session: AsyncSession,
    *,
    github_id: int,
    full_name: str,
    installation_id: int,
    default_branch: str | None = None,
    activate: bool = False,
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
    if activate:  # (re)installed: back in business, whatever the previous status was
        updates["status"] = "active"
        updates["status_changed_at"] = func.now()
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
    lists `repositories_added`. Both register the repositories and start the initial index.
    Lifecycle actions (deleted, suspend, unsuspend, removed) change the repository status;
    repositories that are not `active` are never analysed, but their data is kept."""
    action = payload.get("action")
    installation_id = int(payload["installation"]["id"])
    if action in ("deleted", "suspend"):
        status_ = "uninstalled" if action == "deleted" else "suspended"
        await _set_status(session, Repository.installation_id == installation_id, status_)
        await session.commit()
        return True
    if action == "unsuspend":
        resumed = await _set_status(
            session,
            (Repository.installation_id == installation_id) & (Repository.status == "suspended"),
            "active",
        )
        await session.commit()
        for repository_id in resumed:  # pushes during the suspension were never seen
            await enqueue(queue, "index_repository", repository_id)
        return True
    if action == "removed":
        gone = [int(item["id"]) for item in payload.get("repositories_removed") or []]
        await _set_status(
            session,
            (Repository.installation_id == installation_id) & Repository.github_id.in_(gone),
            "uninstalled",
        )
        await session.commit()
        return True
    if action not in ("created", "added"):
        return False
    listed = payload.get("repositories_added") or payload.get("repositories") or []
    repository_ids = [
        await _upsert_repository(
            session,
            github_id=int(item["id"]),
            full_name=item["full_name"],
            installation_id=installation_id,
            activate=True,
        )
        for item in listed
    ]
    await session.commit()
    for repository_id in repository_ids:
        await enqueue(queue, "index_repository", repository_id)
    return True


async def _set_status(session: AsyncSession, where: Any, status_: str) -> list[uuid.UUID]:
    """Change the status of the matching repositories (only those that are not already there)."""
    result = await session.execute(
        update(Repository)
        .where(where, Repository.status != status_)
        .values(status=status_, status_changed_at=func.now())
        .returning(Repository.id)
    )
    changed = list(result.scalars())
    logger.info("repositories set to %s: %d", status_, len(changed))
    return changed


async def _is_active(session: AsyncSession, repository_id: uuid.UUID) -> bool:
    return (
        await session.scalar(select(Repository.status).where(Repository.id == repository_id))
        == "active"
    )


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
    if not await _is_active(session, repository_id):
        await session.commit()
        return False

    # `commits` holds at most 20 entries; the head commit is always covered.
    commits: dict[str, dict[str, Any]] = {c["id"]: c for c in payload.get("commits") or []}
    head = payload.get("head_commit")
    if head:
        commits.setdefault(head["id"], head)
    if not commits:
        return False

    on_default_branch = ref == f"refs/heads/{repo.get('default_branch', 'main')}"
    for entry in commits.values():
        author = entry.get("author") or {}
        statement = insert(Commit).values(
            repository_id=repository_id,
            sha=entry["id"],
            author=author.get("name") or author.get("username"),
            message=entry.get("message"),
            committed_at=_parse_time(entry.get("timestamp")),
            on_default_branch=on_default_branch,
        )
        # A commit pushed to a feature branch becomes a default-branch commit when its PR is
        # merged (a merge commit pushes the PR commits again); it never goes back.
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["repository_id", "sha"],
                set_={
                    "on_default_branch": or_(
                        func.coalesce(Commit.on_default_branch, false()),
                        statement.excluded.on_default_branch,
                    )
                },
            )
        )
    await session.commit()

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
    if not await _is_active(session, repository_id):
        return False
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
