"""Phase 11.4: confirmations the code has overtaken (R5) and their place in the review queue."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import ApiContext

from rastro_db import create_session_factory
from rastro_db.models import Rule, RuleAnchor, RuleReview
from rastro_db.models import Symbol as SymbolRow

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)


def auth(api: ApiContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


async def get(api: ApiContext, **params: Any) -> dict[str, Any]:
    response = await api.client.get(
        f"/v1/repos/{api.repository_id}/health", params=params, headers=auth(api)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def r5_of(body: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {metric["id"]: metric for metric in body["knowledge"]}["R5"]
    return found


async def seed_code(api: ApiContext) -> dict[str, Any]:
    """Symbols whose code is as it was when confirmed (a), changed since (b), gone (c) or a copy that
    diverged (d); and rules confirmed with the hashes of the code they looked at."""
    ids: dict[str, Any] = {}
    async with create_session_factory(api.engine)() as session, session.begin():

        def symbol(path: str, name: str, body_hash: str, status: str = "active") -> SymbolRow:
            row = SymbolRow(
                repository_id=api.repository_id,
                file_path=path,
                qualified_name=name,
                kind="function",
                body_hash=body_hash,
                last_seen_sha="s",
                status=status,
            )
            session.add(row)
            return row

        a, b = symbol("src/a.ts", "f", "h1"), symbol("src/b.ts", "g", "h2-new")
        c, d = symbol("src/c.ts", "h", "x", "removed"), symbol("src/d.ts", "k", "k2")

        async def rule(
            name: str,
            anchors: list[tuple[SymbolRow, str]],
            hashes: dict[str, str] | None,
            *,
            reviewed: bool = True,
            revision: int = 1,
            validated_revision: int | None = 1,
        ) -> None:
            row = Rule(
                repository_id=api.repository_id,
                statement=f"rule {name}",
                rationale="a reason",  # every rule has one: only the code can put it in the queue
                confidence=0.9,
                revision=revision,
                validated_revision=validated_revision,
            )
            session.add(row)
            await session.flush()
            ids[name] = row.id
            session.add_all(
                [RuleAnchor(rule_id=row.id, symbol_id=s.id, role=role) for s, role in anchors]
            )
            if reviewed:
                session.add(
                    RuleReview(
                        rule_id=row.id,
                        rule_revision=1,
                        verdict="confirmed",
                        reviewer="ana",
                        reviewer_source="github",
                        anchor_hashes=hashes,
                        created_at=NOW - timedelta(days=3),
                    )
                )

        await rule("same", [(a, "primary")], {"src/a.ts::f": "h1"})
        await rule("changed", [(b, "primary")], {"src/b.ts::g": "h2-old"})
        await rule("gone", [(c, "primary")], {"src/c.ts::h": "x"})
        await rule(
            "copy",
            [(a, "primary"), (d, "duplicate")],
            {"src/a.ts::f": "h1", "src/d.ts::k": "k1"},  # the copy diverged
        )
        await rule("unknown", [(a, "primary")], None)  # confirmed before the hashes were recorded
        await rule("none", [(a, "primary")], None, reviewed=False)  # confirmed, no review row left
        await rule(
            "stale", [(b, "primary")], {"src/b.ts::g": "h2-old"}, revision=2
        )  # not valid now
        # re-confirmed after the code changed: the newest confirmation is the one that counts
        await rule("reconfirmed", [(b, "primary")], {"src/b.ts::g": "h2-old"})
        session.add(
            RuleReview(
                rule_id=ids["reconfirmed"],
                rule_revision=1,
                verdict="confirmed",
                reviewer="bo",
                reviewer_source="github",
                anchor_hashes={"src/b.ts::g": "h2-new"},
                created_at=NOW - timedelta(days=1),
            )
        )
    return ids


async def test_r5_counts_confirmed_rules_whose_code_is_no_longer_what_was_confirmed(
    api: ApiContext,
) -> None:
    await seed_code(api)

    r5 = r5_of(await get(api))

    # known: same, changed, gone, copy, reconfirmed. changed: the edited function and the copy that
    # diverged. The removed symbol is skipped and the re-confirmation is judged on its own hashes.
    assert (r5["numerator"], r5["denominator"]) == (2, 5)
    assert r5["detail"] == {
        "unknown": 2
    }  # a confirmation without hashes, and one without a review row
    assert r5["small_sample"] is True


async def test_rules_the_code_has_overtaken_go_to_the_review_queue_with_that_reason(
    api: ApiContext,
) -> None:
    ids = await seed_code(api)

    queue = (await get(api))["queue"]

    by_rule = {item["rule_id"]: item["reasons"] for item in queue}
    # "stale" is there for the older reason (the rule changed after it was confirmed); the rest have
    # a reason and a confirmation that still holds, so there is nothing to do
    assert set(by_rule) == {str(ids["changed"]), str(ids["copy"]), str(ids["stale"])}
    assert by_rule[str(ids["changed"])] == ["validation_outdated_by_code"]
    assert by_rule[str(ids["copy"])] == ["validation_outdated_by_code"]
    assert by_rule[str(ids["stale"])] == ["validation_outdated"]


async def test_r5_is_not_available_when_no_confirmation_recorded_its_hashes(
    api: ApiContext,
) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(
            Rule(
                repository_id=api.repository_id,
                statement="old",
                confidence=0.9,
                revision=1,
                validated_revision=1,
            )
        )

    r5 = r5_of(await get(api))

    assert r5["available"] is False and r5["value"] is None
    assert "before phase 11.4" in r5["na_reason"]
    assert r5["detail"] == {"unknown": 1}


async def test_a_queue_without_rules_to_look_at_again_still_orders_by_the_old_criteria(
    api: ApiContext,
) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(Rule(repository_id=api.repository_id, statement="no reason", confidence=0.9))

    queue = (await get(api))["queue"]

    assert [item["reasons"] for item in queue] == [["no_rationale", "not_validated"]]
