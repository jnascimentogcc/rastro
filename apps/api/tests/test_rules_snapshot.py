import uuid

import httpx
import pytest
from api_support import ApiContext
from sqlalchemy import select, update
from test_rules import DISCOUNT, IMPLANTS, SHA, WHEN, auth, seed, url

from rastro_api.routers import rules as rules_router
from rastro_db import create_session_factory
from rastro_db.models import Commit, Rule
from rastro_db.models import Symbol as SymbolRow

pytestmark = pytest.mark.db


async def snapshot(api: ApiContext, **headers: str) -> httpx.Response:
    return await api.client.get(url(api, "/snapshot"), headers={**auth(api), **headers})


async def test_snapshot_has_active_rules_with_primary_anchor_first(api: ApiContext) -> None:
    await seed(api)

    response = await snapshot(api)

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    by_statement = {rule["statement"]: rule for rule in body["rules"]}
    assert set(by_statement) == {IMPLANTS, DISCOUNT}  # the orphaned rule gives no advice
    assert by_statement[IMPLANTS]["anchors"] == [
        {"file_path": "src/agenda.ts", "qualified_name": "validate", "role": "primary"},
        {"file_path": "src/calendar.ts", "qualified_name": "isAllowed", "role": "duplicate"},
    ]
    assert by_statement[IMPLANTS]["rationale"] == "Dr. Mendes needs 48h of follow-up"
    assert by_statement[IMPLANTS]["validation"] == "unvalidated"
    assert "origin" not in by_statement[IMPLANTS]  # no quotes, no session data


async def test_snapshot_leaves_out_rejected_rules_and_removed_symbols(api: ApiContext) -> None:
    ids = await seed(api)
    async with create_session_factory(api.engine)() as session, session.begin():
        await session.execute(
            update(Rule).where(Rule.id == ids["discount"]).values(status="rejected")
        )
        await session.execute(
            update(SymbolRow)
            .where(SymbolRow.file_path == "src/calendar.ts")
            .values(status="removed")
        )

    body = (await snapshot(api)).json()

    [rule] = body["rules"]
    assert rule["statement"] == IMPLANTS
    assert [a["file_path"] for a in rule["anchors"]] == [
        "src/agenda.ts"
    ]  # the removed copy is gone


async def test_snapshot_reports_the_default_branch_head(api: ApiContext) -> None:
    await seed(api)
    assert (await snapshot(api)).json()["head_sha"] is None

    async with create_session_factory(api.engine)() as session, session.begin():
        await session.execute(
            update(Commit).where(Commit.sha == SHA).values(on_default_branch=True)
        )
        session.add(
            Commit(
                repository_id=api.repository_id,
                sha="b" * 40,
                committed_at=WHEN.replace(year=2025),
                on_default_branch=True,
            )
        )
        session.add(
            Commit(
                repository_id=api.repository_id,
                sha="c" * 40,
                committed_at=WHEN.replace(year=2027),
                on_default_branch=False,  # a feature branch is not the head
            )
        )

    assert (await snapshot(api)).json()["head_sha"] == SHA


async def test_unchanged_snapshot_answers_304_and_any_change_breaks_the_etag(
    api: ApiContext,
) -> None:
    ids = await seed(api)
    first = await snapshot(api)
    etag = first.headers["etag"]

    again = await snapshot(api, **{"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert (await snapshot(api, **{"If-None-Match": '"other", ' + etag})).status_code == 304
    assert (await snapshot(api, **{"If-None-Match": '"other"'})).status_code == 200

    # a human confirms a rule: nothing about row counts changes, the content does
    confirmed = await api.client.post(
        url(api, f"/{ids['implants']}/reviews"),
        json={"verdict": "confirmed", "reviewer": "ana"},
        headers=auth(api),
    )
    assert confirmed.status_code == 200
    after_review = await snapshot(api, **{"If-None-Match": etag})
    assert after_review.status_code == 200
    assert after_review.headers["etag"] != etag

    # an anchor moves (resync) without touching the rule row
    new_etag = after_review.headers["etag"]
    async with create_session_factory(api.engine)() as session, session.begin():
        pricing = await session.scalar(
            select(SymbolRow).where(SymbolRow.file_path == "src/pricing.ts")
        )
        assert pricing is not None
        pricing.file_path = "src/prices.ts"
    moved = await snapshot(api, **{"If-None-Match": new_etag})
    assert moved.status_code == 200


async def test_snapshot_says_when_it_was_cut(
    api: ApiContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(api)
    monkeypatch.setattr(rules_router, "SNAPSHOT_MAX_RULES", 1)

    body = (await snapshot(api)).json()

    assert body["truncated"] is True
    assert len(body["rules"]) == 1


async def test_snapshot_needs_the_own_repository_token(api: ApiContext) -> None:
    await seed(api)

    anonymous = await api.client.get(url(api, "/snapshot"))
    foreign = await api.client.get(f"/v1/repos/{uuid.uuid4()}/rules/snapshot", headers=auth(api))

    assert anonymous.status_code == 401
    assert foreign.status_code == 404
