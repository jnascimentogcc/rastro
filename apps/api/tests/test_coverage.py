"""Phase 11.3: what a session edited that already had rules, and whether the hook announced it first."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import ApiContext
from sqlalchemy import func, select

from rastro_db import create_session_factory
from rastro_db.models import AgentSession, Rule, RuleAnchor, SessionCoverage
from rastro_db.models import Symbol as SymbolRow

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)


def stamp(delta: float = 0, **extra: float) -> str:
    return (T0 + timedelta(seconds=delta, **extra)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def auth(api: ApiContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


async def seed(api: ApiContext) -> None:
    """src/a.ts and src/d.ts have a rule that existed before the session; src/b.ts only gets one
    ten minutes into it; src/c.ts has none."""
    async with create_session_factory(api.engine)() as session, session.begin():
        for path, created in [
            ("src/a.ts", T0 - timedelta(days=5)),
            ("src/d.ts", T0 - timedelta(days=5)),
            ("src/b.ts", T0 + timedelta(minutes=10)),
        ]:
            symbol = SymbolRow(
                repository_id=api.repository_id,
                file_path=path,
                qualified_name="f",
                kind="function",
                body_hash=f"h-{path}",
                last_seen_sha="s",
                status="active",
            )
            rule = Rule(
                repository_id=api.repository_id,
                statement=f"rule for {path}",
                confidence=0.9,
                created_at=created,
            )
            session.add_all([symbol, rule])
            await session.flush()
            session.add(RuleAnchor(rule_id=rule.id, symbol_id=symbol.id, role="primary"))


def edit(path: str, delta: float) -> dict[str, Any]:
    return {
        "role": "assistant",
        "kind": "edit",
        "text": f"Edit {path}",
        "timestamp": stamp(delta),
        "files": [path],
    }


def announced(path: str, delta: float, via: str = "read") -> dict[str, Any]:
    return {"at": stamp(delta), "agent": None, "file": path, "rule_ids": ["r1"], "via": via}


def start(delta: float = -60) -> dict[str, Any]:
    return {"at": stamp(delta), "agent": None, "file": "", "rule_ids": [], "via": "start"}


def capture(
    sha: str, excerpts: list[dict[str, Any]], consulted: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        "commit_sha": sha * 40,
        "session_ref": "sess-1",
        "agent": "claude-code",
        "git_branch": "main",
        "excerpts": excerpts,
        "consulted": consulted,
        **extra,
    }


async def post(api: ApiContext, payload: dict[str, Any]) -> int:
    response = await api.client.post("/v1/sessions", json=payload, headers=auth(api))
    return response.status_code


async def coverage(api: ApiContext) -> list[SessionCoverage]:
    async with create_session_factory(api.engine)() as session:
        return list(
            await session.scalars(
                select(SessionCoverage).where(SessionCoverage.repository_id == api.repository_id)
            )
        )


async def test_only_files_that_already_had_rules_count_and_only_announcements_before_the_edit_help(
    api: ApiContext,
) -> None:
    await seed(api)
    excerpts = [
        edit("src/a.ts", 0),
        edit("src/b.ts", 30),
        edit("src/c.ts", 60),
        edit("src/d.ts", 90),
    ]
    consulted = [
        start(),
        announced("src/a.ts", -30),  # announced before the first edit
        announced("src/b.ts", -30),  # announced, but its rule did not exist yet
        announced("src/d.ts", 200),  # announced after the edit: too late
    ]

    assert await post(api, capture("a", excerpts, consulted, agent_hook=True)) == 201

    [row] = await coverage(api)
    assert (row.edited_ruled, row.consulted_first) == (
        2,
        1,
    )  # a.ts and d.ts; b.ts and c.ts left out
    assert row.files == [
        {"file": "src/a.ts", "consulted": True},
        {"file": "src/d.ts", "consulted": False},
    ]
    assert row.hook_active is True
    assert row.started_at == T0


async def test_a_path_written_the_windows_way_or_in_another_case_still_matches(
    api: ApiContext,
) -> None:
    await seed(api)

    await post(api, capture("a", [edit("src/a.ts", 0)], [announced("SRC\\A.ts", -5)]))

    [row] = await coverage(api)
    assert row.consulted_first == 1


async def test_a_session_with_several_commits_is_judged_as_a_whole(api: ApiContext) -> None:
    await seed(api)
    # The announcements travelled with the first capture; the second commit has none of its own.
    await post(
        api,
        capture(
            "a",
            [edit("src/a.ts", 0)],
            [start(), announced("src/a.ts", -30), announced("src/d.ts", -20)],
            agent_hook=True,
        ),
    )
    await post(api, capture("b", [edit("src/d.ts", 300)], [], agent_hook=False))

    [row] = await coverage(api)  # one row for the session, not one per capture
    assert (row.edited_ruled, row.consulted_first) == (
        2,
        2,
    )  # d.ts was announced by the first capture
    assert row.hook_active is True  # a later capture without the start line does not undo it


async def test_resending_a_capture_recomputes_instead_of_adding(api: ApiContext) -> None:
    await seed(api)
    payload = capture("a", [edit("src/a.ts", 0)], [announced("src/a.ts", -5)])

    await post(api, payload)
    await post(api, payload)

    rows = await coverage(api)
    assert len(rows) == 1 and rows[0].edited_ruled == 1


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (True, False, True),
        (False, None, False),
        (None, None, None),
        (None, True, True),
        (False, False, False),
    ],
)
async def test_whether_the_hook_ran_is_true_if_any_capture_proved_it(
    api: ApiContext, first: bool | None, second: bool | None, expected: bool | None
) -> None:
    await seed(api)

    await post(api, capture("a", [edit("src/a.ts", 0)], [], agent_hook=first))
    await post(api, capture("b", [edit("src/d.ts", 5)], [], agent_hook=second))

    [row] = await coverage(api)
    assert row.hook_active is expected


async def test_the_hook_flag_is_kept_on_each_captured_row(api: ApiContext) -> None:
    await seed(api)

    await post(api, capture("a", [edit("src/a.ts", 0)], [], agent_hook=True))

    async with create_session_factory(api.engine)() as session:
        flag = await session.scalar(
            select(AgentSession.hook_active).where(AgentSession.repository_id == api.repository_id)
        )
    assert flag is True


async def test_a_session_that_edited_nothing_with_rules_has_a_row_with_zeros(
    api: ApiContext,
) -> None:
    await seed(api)

    await post(api, capture("a", [edit("src/c.ts", 0)], [start()], agent_hook=True))

    [row] = await coverage(api)
    assert (row.edited_ruled, row.consulted_first, row.files) == (0, 0, [])
    assert row.hook_active is True  # still proof that the hook ran


async def test_the_start_marker_needs_no_file_but_a_real_announcement_does(api: ApiContext) -> None:
    await seed(api)

    assert await post(api, capture("a", [edit("src/a.ts", 0)], [start()])) == 201
    assert await post(api, capture("b", [], [{**announced("src/a.ts", 0), "file": ""}])) == 422
    assert await post(api, capture("c", [], [{**announced("src/a.ts", 0), "via": "other"}])) == 422


async def test_coverage_is_stored_per_session_not_per_capture(api: ApiContext) -> None:
    await seed(api)
    await post(api, capture("a", [edit("src/a.ts", 0)], []))
    await post(api, {**capture("b", [edit("src/d.ts", 5)], []), "session_ref": "sess-2"})

    async with create_session_factory(api.engine)() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SessionCoverage)
            .where(SessionCoverage.repository_id == api.repository_id)
        )
    assert count == 2
