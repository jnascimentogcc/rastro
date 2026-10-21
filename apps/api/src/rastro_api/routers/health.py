"""The repository health report (phase 11): counts only, no model call.

Authenticated with the repository token; the repository id in the URL must be the token's own.
Nothing here writes, and nothing calls `app.state.llm`: the report is plain SQL (spec D5).
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from rastro_api.deps import CurrentRepository, DbSession
from rastro_api.schemas import HealthHistoryOut, HealthOut, HealthSnapshotOut
from rastro_db import health, health_snapshots

router = APIRouter(prefix="/v1/repos/{repo_id}/health", tags=["health"])


@router.get("", response_model=HealthOut)
async def get_health(
    repo_id: uuid.UUID,
    repository: CurrentRepository,
    session: DbSession,
    window: int = Query(
        default=30, ge=7, le=365, description="Days covered by the process and pipeline figures"
    ),
    exclude: Annotated[
        list[str] | None, Query(description="Extra globs to leave out of the zones")
    ] = None,
    zones: bool = Query(default=True),
    queue: int = Query(
        default=5, ge=0, le=health.MAX_QUEUE, description="Size of the review queue"
    ),
    explain: bool = Query(default=False, description="Include the definition of every figure"),
) -> HealthOut:
    if repo_id != repository.id:
        # Same answer as for a repository that does not exist: do not reveal other tenants.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    report = await health.build_report(
        session,
        repository.id,
        window_days=window,
        excludes=exclude or [],
        queue_size=queue,
        include_zones=zones,
    )
    payload = asdict(report)
    earlier = await health_snapshots.compared_to(session, repository.id, window_days=window)
    if earlier is not None:
        payload["compared_to"] = {
            "taken_on": earlier.taken_on,
            "days_ago": (datetime.now(UTC).date() - earlier.taken_on).days,
            "metrics": health_snapshots.headline(earlier, everything=True),
        }
    payload["definitions"] = health.DEFINITIONS if explain else None
    return HealthOut.model_validate(payload)


@router.get("/history", response_model=HealthHistoryOut)
async def get_health_history(
    repo_id: uuid.UUID,
    repository: CurrentRepository,
    session: DbSession,
    limit: int = Query(default=30, ge=1, le=365, description="Days to list, most recent first"),
) -> HealthHistoryOut:
    """The headline figures of the daily snapshots. Read only: snapshots are taken by the worker."""
    if repo_id != repository.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    rows = await health_snapshots.history(session, repository.id, limit=limit)
    return HealthHistoryOut(
        snapshots=[
            HealthSnapshotOut.model_validate(
                {
                    "taken_on": row.taken_on,
                    "window_days": row.window_days,
                    "definitions_version": row.definitions_version,
                    "head_sha": row.head_sha,
                    "figures": health_snapshots.headline(row),
                }
            )
            for row in rows
        ]
    )
