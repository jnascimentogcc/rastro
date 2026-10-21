"""Daily snapshots of the health report and the comparison with an earlier one (phase 11.5).

A snapshot keeps the figures only (numerators, denominators, flags), never rule text. It is taken
without a model and compared only with snapshots computed the same way: same `definitions_version`
and same window, otherwise a changed formula would read as a trend (spec D5).
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db.health import DEFINITIONS_VERSION, HealthReport, build_report
from rastro_db.models import HealthSnapshot

SNAPSHOT_WINDOW_DAYS = 30
# What `--history` shows per day: the figures a trend is read from.
HEADLINE = ("R1", "R2", "R3", "R4", "R5", "P2", "P3", "C1", "C2")


def figures(report: HealthReport) -> dict[str, dict[str, Any]]:
    """The metrics of a report by id, as stored in a snapshot."""
    return {
        metric["id"]: metric
        for group in (report.knowledge, report.process, report.intent, report.agents)
        for metric in map(asdict, group)
    }


async def take_snapshot(
    session: AsyncSession, repository_id: uuid.UUID, *, now: datetime | None = None
) -> HealthSnapshot:
    """Store today's figures; a second run on the same day replaces the first."""
    now = now or datetime.now(UTC)
    report = await build_report(
        session,
        repository_id,
        window_days=SNAPSHOT_WINDOW_DAYS,
        include_zones=False,
        queue_size=0,
        now=now,
    )
    values = {
        "repository_id": repository_id,
        "taken_on": now.date(),
        "taken_at": now,
        "window_days": report.window_days,
        "definitions_version": report.definitions_version,
        "head_sha": report.head_sha,
        "metrics": figures(report),
    }
    row_id = await session.scalar(
        insert(HealthSnapshot)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["repository_id", "taken_on"],
            set_={
                key: value
                for key, value in values.items()
                if key not in ("repository_id", "taken_on")
            },
        )
        .returning(HealthSnapshot.id)
    )
    snapshot = await session.get(HealthSnapshot, row_id, populate_existing=True)
    assert snapshot is not None
    return snapshot


async def compared_to(
    session: AsyncSession,
    repository_id: uuid.UUID,
    *,
    window_days: int,
    now: datetime | None = None,
) -> HealthSnapshot | None:
    """The oldest snapshot inside the window that was computed the same way as a report with this
    window today; None when there is none (never a snapshot from another definition)."""
    today = (now or datetime.now(UTC)).date()
    return await session.scalar(
        select(HealthSnapshot)
        .where(
            HealthSnapshot.repository_id == repository_id,
            HealthSnapshot.definitions_version == DEFINITIONS_VERSION,
            HealthSnapshot.window_days == window_days,
            HealthSnapshot.taken_on >= today - timedelta(days=window_days),
            HealthSnapshot.taken_on < today,
        )
        .order_by(HealthSnapshot.taken_on)
        .limit(1)
    )


async def history(
    session: AsyncSession, repository_id: uuid.UUID, *, limit: int = 30
) -> list[HealthSnapshot]:
    """Most recent first."""
    rows = await session.scalars(
        select(HealthSnapshot)
        .where(HealthSnapshot.repository_id == repository_id)
        .order_by(HealthSnapshot.taken_on.desc())
        .limit(limit)
    )
    return list(rows)


def headline(snapshot: HealthSnapshot, *, everything: bool = False) -> dict[str, dict[str, Any]]:
    """The figures of a snapshot without their detail: the few a trend is read from, or all."""
    return {
        metric_id: {
            key: metric.get(key)
            for key in ("numerator", "denominator", "value", "available", "small_sample")
        }
        for metric_id, metric in snapshot.metrics.items()
        if everything or metric_id in HEADLINE
    }
