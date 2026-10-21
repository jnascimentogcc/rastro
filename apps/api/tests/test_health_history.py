"""Phase 11.5: snapshots, the comparison with an earlier day and the history route."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import ApiContext
from sqlalchemy import func, select

from rastro_db import create_session_factory, health_snapshots
from rastro_db.health import DEFINITIONS_VERSION
from rastro_db.models import HealthSnapshot, Rule

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)


def auth(api: ApiContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


async def get(api: ApiContext, path: str = "", **params: Any) -> dict[str, Any]:
    response = await api.client.get(
        f"/v1/repos/{api.repository_id}/health{path}", params=params, headers=auth(api)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def snapshot_on(
    api: ApiContext,
    days_ago: int,
    *,
    version: int = DEFINITIONS_VERSION,
    window: int = 30,
    active: int = 1,
) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(
            HealthSnapshot(
                repository_id=api.repository_id,
                taken_on=(NOW - timedelta(days=days_ago)).date(),
                window_days=window,
                definitions_version=version,
                head_sha=None,
                metrics={
                    "R1": {
                        "id": "R1",
                        "numerator": active,
                        "available": True,
                        "small_sample": False,
                    },
                    "R3": {
                        "id": "R3",
                        "numerator": 0,
                        "denominator": active,
                        "value": 0.0,
                        "available": True,
                        "small_sample": True,
                    },
                },
            )
        )


async def test_one_snapshot_per_day_holds_figures_and_no_rule_text(api: ApiContext) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(
            Rule(repository_id=api.repository_id, statement="secret statement", confidence=0.9)
        )
    factory = create_session_factory(api.engine)
    for _ in range(2):  # a second run on the same day replaces the first
        async with factory() as session, session.begin():
            await health_snapshots.take_snapshot(session, api.repository_id, now=NOW)

    async with factory() as session:
        rows = (
            await session.scalars(
                select(HealthSnapshot).where(HealthSnapshot.repository_id == api.repository_id)
            )
        ).all()
    [row] = rows
    assert row.window_days == 30 and row.definitions_version == DEFINITIONS_VERSION
    assert row.metrics["R1"]["numerator"] == 1
    assert "secret statement" not in str(row.metrics)


async def test_the_report_is_compared_with_the_oldest_snapshot_of_the_window(
    api: ApiContext,
) -> None:
    await snapshot_on(api, 20, active=2)
    await snapshot_on(api, 5, active=3)

    body = await get(api, window=30)

    assert body["compared_to"]["days_ago"] == 20
    assert body["compared_to"]["metrics"]["R1"]["numerator"] == 2


async def test_snapshots_computed_another_way_are_never_compared(api: ApiContext) -> None:
    await snapshot_on(api, 10, version=DEFINITIONS_VERSION + 1)  # a formula changed since
    await snapshot_on(api, 12, window=60)  # another window
    await snapshot_on(api, 40)  # too old for the window

    assert (await get(api, window=30))["compared_to"] is None


async def test_without_snapshots_the_report_says_nothing_to_compare(api: ApiContext) -> None:
    assert (await get(api))["compared_to"] is None


async def test_the_history_lists_headline_figures_most_recent_first(api: ApiContext) -> None:
    await snapshot_on(api, 3, active=4)
    await snapshot_on(api, 1, active=6)

    body = await get(api, "/history")

    assert [s["figures"]["R1"]["numerator"] for s in body["snapshots"]] == [6, 4]
    assert set(body["snapshots"][0]["figures"]) <= set(health_snapshots.HEADLINE)


async def test_the_report_and_the_history_write_nothing(api: ApiContext) -> None:
    await get(api)
    await get(api, "/history")

    async with create_session_factory(api.engine)() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(HealthSnapshot)
            .where(HealthSnapshot.repository_id == api.repository_id)
        )
    assert count == 0


async def test_the_history_of_another_repository_is_not_found(api: ApiContext) -> None:
    response = await api.client.get(
        "/v1/repos/00000000-0000-0000-0000-000000000000/health/history", headers=auth(api)
    )
    assert response.status_code == 404
