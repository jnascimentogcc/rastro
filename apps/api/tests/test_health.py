import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import ApiContext
from sqlalchemy import text

from rastro_db import create_session_factory, generate_token, hash_token, health
from rastro_db.models import Commit, Repository, Rule, RuleAnchor, RuleReview, SymbolCall
from rastro_db.models import Symbol as SymbolRow

NOW = datetime.now(UTC)


def auth(api: ApiContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


def url(api: ApiContext) -> str:
    return f"/v1/repos/{api.repository_id}/health"


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("path", "zone"),
    [
        ("src/domain/scheduling.ts", "src/domain"),
        ("src/api/routes/users.ts", "src/api"),
        ("src/index.ts", "src"),
        ("index.ts", "."),
        ("src\\domain\\a.ts", "src/domain"),
    ],
)
def test_a_file_belongs_to_its_first_two_directories(path: str, zone: str) -> None:
    assert health.zone_of(path) == zone


@pytest.mark.parametrize(
    "path",
    [
        "tests/a.ts",
        "packages/x/tests/a.ts",
        "src/a.test.ts",
        "src/a.spec.ts",
        "node_modules/x/index.js",
        "src/types.d.ts",
        "dist/main.js",
        "src/generated/api.ts",
    ],
)
def test_tests_dependencies_and_generated_code_are_left_out_by_default(path: str) -> None:
    assert health.is_excluded(path, health.DEFAULT_EXCLUDES)


def test_ordinary_source_files_are_not_left_out() -> None:
    assert not health.is_excluded("src/domain/scheduling.ts", health.DEFAULT_EXCLUDES)
    assert health.is_excluded("src/legacy/a.ts", ("src/legacy/*",))


def test_a_fraction_without_a_denominator_is_not_available_instead_of_zero() -> None:
    metric = health.ratio("X", "x", 0, 0, na_reason="nothing to divide")

    assert not metric.available and metric.value is None and metric.na_reason == "nothing to divide"


def test_a_fraction_below_ten_cases_is_flagged_as_a_small_sample() -> None:
    assert health.ratio("X", "x", 1, 9, na_reason="").small_sample
    assert not health.ratio("X", "x", 1, 10, na_reason="").small_sample


# --------------------------------------------------------------------------- the report


async def seed(api: ApiContext) -> dict[str, uuid.UUID]:
    """Five active rules, an orphaned and a rejected one; zones, callers and commits chosen so every
    figure can be computed by hand (see `test_the_figures_match_the_hand_count`)."""
    ids: dict[str, uuid.UUID] = {}
    async with create_session_factory(api.engine)() as session, session.begin():

        def symbol(path: str, name: str, status: str = "active") -> SymbolRow:
            row = SymbolRow(
                repository_id=api.repository_id,
                file_path=path,
                qualified_name=name,
                kind="function",
                body_hash=f"h-{path}-{name}",
                start_line=1,
                end_line=5,
                last_seen_sha="s",
                status=status,
            )
            session.add(row)
            return row

        fa, fc, helper = (
            symbol("src/domain/a.ts", "fa"),
            symbol("src/domain/c.ts", "fc"),
            symbol("src/domain/c.ts", "helper"),
        )
        fd, fe = symbol("src/api/d.ts", "fd"), symbol("src/api/e.ts", "fe")
        callers = [symbol(f"src/api/call{i}.ts", f"c{i}") for i in range(3)]
        old = symbol("src/domain/old.ts", "gone", "removed")
        symbol("tests/x.test.ts", "t")
        symbol("src/api/types.d.ts", "decl")

        def rule(name: str, **values: Any) -> Rule:
            defaults: dict[str, Any] = dict(
                repository_id=api.repository_id,
                statement=f"Rule {name}",
                confidence=0.9,
                revision=1,
                created_at=NOW - timedelta(days=5),
                updated_at=NOW - timedelta(days=1),
            )
            row = Rule(**{**defaults, **values})
            session.add(row)
            ids[name] = row.id
            return row

        a = rule(
            "A",
            rationale="why a",
            rationale_source="conversation",
            validated_revision=1,
            created_at=NOW - timedelta(days=100),
        )
        b = rule("B", rationale="why b", rationale_source="human", revision=2, validated_revision=2)
        c = rule("C", rationale=None, created_at=NOW - timedelta(days=40))
        d = rule(
            "D",
            rationale="why d",
            rationale_source="conversation",
            revision=2,
            validated_revision=1,
            created_at=NOW - timedelta(days=10),
        )
        e = rule("E", rationale=None, validated_revision=1)
        f = rule("F", status="orphaned", rationale="x")
        g = rule("G", status="rejected", rationale="y")
        await session.flush()
        ids.update(A=a.id, B=b.id, C=c.id, D=d.id, E=e.id, F=f.id, G=g.id)
        session.add_all(
            [
                RuleAnchor(rule_id=a.id, symbol_id=fa.id, role="primary"),
                RuleAnchor(rule_id=b.id, symbol_id=fa.id, role="duplicate"),  # same zone as A
                RuleAnchor(rule_id=c.id, symbol_id=fc.id, role="primary"),
                RuleAnchor(rule_id=d.id, symbol_id=fd.id, role="primary"),
                RuleAnchor(rule_id=e.id, symbol_id=fe.id, role="primary"),
                RuleAnchor(rule_id=f.id, symbol_id=old.id, role="primary"),
                RuleReview(
                    rule_id=a.id,
                    rule_revision=1,
                    verdict="confirmed",
                    reviewer="ana",
                    reviewer_source="github",
                ),
                RuleReview(
                    rule_id=b.id,
                    rule_revision=2,
                    verdict="confirmed",
                    reviewer="bo",
                    reviewer_source="cli",
                ),
                *[SymbolCall(caller_id=caller.id, callee_id=fc.id) for caller in callers],
                SymbolCall(caller_id=helper.id, callee_id=fc.id),  # a fourth caller of fc
            ]
        )
        for sha, status, age, default in [
            ("1" * 40, "analysis_failed", 5, True),
            ("2" * 40, "skipped_budget", 40, True),  # outside the 30-day window
            ("3" * 40, "skipped_quota", 2, True),
            ("4" * 40, "awaiting_merge", 20, False),  # waited more than 14 days
            ("5" * 40, "awaiting_merge", 3, False),  # still fresh
            ("6" * 40, "analyzed", 1, True),
        ]:
            session.add(
                Commit(
                    repository_id=api.repository_id,
                    sha=sha,
                    committed_at=NOW - timedelta(days=age),
                    analysis_status=status,
                    on_default_branch=default,
                )
            )
    return ids


async def get(api: ApiContext, **params: Any) -> dict[str, Any]:
    response = await api.client.get(url(api), params=params, headers=auth(api))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def metrics(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {metric["id"]: metric for metric in body["knowledge"]}


@pytest.mark.db
async def test_the_figures_match_the_hand_count(api: ApiContext) -> None:
    await seed(api)

    body = await get(api)
    m = metrics(body)

    assert m["R1"]["detail"] == {"active": 5, "orphaned": 1, "rejected": 1, "superseded": 0}
    assert (m["R2"]["numerator"], m["R2"]["denominator"]) == (3, 5)  # A, B, D have a reason
    assert m["R2"]["detail"] == {"from_conversation": 2, "written_by_people": 1}
    assert (m["R3"]["numerator"], m["R3"]["denominator"]) == (3, 5)  # A, B, E are confirmed
    assert (m["R3b"]["numerator"], m["R3b"]["denominator"]) == (1, 3)  # only A by GitHub
    assert m["R3b"]["detail"] == {"unverified": 2}  # B by the CLI, E without a review row
    assert (m["R4"]["numerator"], m["R4"]["denominator"]) == (
        1,
        4,
    )  # D changed after its confirmation
    assert m["R6"]["numerator"] == 2  # C and D have no valid confirmation
    assert m["R6"]["detail"] == {"oldest_days": 40, "median_days": 25}
    assert body["definitions_version"] == health.DEFINITIONS_VERSION
    assert body["head_sha"] is not None
    assert all(m[key]["small_sample"] for key in ("R2", "R3", "R3b", "R4"))  # fewer than 10 cases


@pytest.mark.db
async def test_zones_group_by_two_directories_and_skip_tests_and_declarations(
    api: ApiContext,
) -> None:
    await seed(api)

    zones = {zone["name"]: zone for zone in (await get(api))["zones"]}

    assert set(zones) == {
        "src/api",
        "src/domain",
    }  # tests/ and *.d.ts left out, removed symbols too
    assert zones["src/domain"] == {
        "name": "src/domain",
        "symbols": 3,  # fa, fc, helper
        "symbols_with_rule": 2,  # fa, fc have a primary anchor of an active rule
        "rules": 3,  # A, B (duplicate anchor) and C; the orphaned F is not counted
        "validated": 2,  # A and B
        "without_reason": 1,  # C
    }
    assert zones["src/api"] == {
        "name": "src/api",
        "symbols": 5,  # fd, fe and the three callers
        "symbols_with_rule": 2,
        "rules": 2,  # D and E
        "validated": 1,  # E; D's confirmation is outdated
        "without_reason": 1,  # E
    }


@pytest.mark.db
async def test_extra_excludes_and_the_zones_switch(api: ApiContext) -> None:
    await seed(api)

    without_api = await get(api, exclude=["src/api/*"])
    no_zones = await get(api, zones="false")

    assert [zone["name"] for zone in without_api["zones"]] == ["src/domain"]
    assert no_zones["zones"] == []


@pytest.mark.db
async def test_the_queue_puts_rules_without_a_reason_first_and_says_why(api: ApiContext) -> None:
    ids = await seed(api)

    queue = (await get(api))["queue"]

    assert [item["rule_id"] for item in queue] == [str(ids["C"]), str(ids["E"]), str(ids["D"])]
    assert queue[0]["reasons"] == ["no_rationale", "not_validated"]
    assert queue[0]["callers"] == 4  # three callers plus the helper
    assert queue[1]["reasons"] == ["no_rationale"]  # confirmed, but nobody knows why
    assert queue[2]["reasons"] == ["validation_outdated"]
    assert queue[0]["command"] == f"rastro rule {str(ids['C'])[:6]} --confirm"
    # confirmed and only the reason is missing: confirming again would change nothing
    assert queue[1]["command"] == f'rastro rule {str(ids["E"])[:6]} --why "<the reason>"'
    assert str(ids["A"]) not in {
        item["rule_id"] for item in queue
    }  # reason and confirmation: nothing to do


@pytest.mark.db
async def test_the_queue_can_be_shortened_or_switched_off(api: ApiContext) -> None:
    await seed(api)

    assert len((await get(api, queue=1))["queue"]) == 1
    assert (await get(api, queue=0))["queue"] == []


@pytest.mark.db
async def test_pipeline_problems_open_the_report_with_a_warning(api: ApiContext) -> None:
    await seed(api)

    body = await get(api)

    assert body["pipeline"] == {
        "analysis_failed": 1,
        "skipped_budget": 0,  # 40 days ago, outside the 30-day window
        "skipped_quota": 1,
        "awaiting_merge": 1,  # only the one waiting for more than 14 days
    }
    [warning] = body["warnings"]
    assert "1 commit(s) were not analysed" in warning
    assert "1 commit(s) failed analysis" in warning
    assert "waited for a merge" in warning and "may be out of date" in warning
    wider = await get(api, window=60)
    assert wider["pipeline"]["skipped_budget"] == 1


@pytest.mark.db
async def test_an_empty_repository_gives_not_available_never_zero_percent(api: ApiContext) -> None:
    body = await get(api)
    m = metrics(body)

    for key in ("R2", "R3", "R3b", "R4"):
        assert m[key]["available"] is False and m[key]["value"] is None
        assert m[key]["na_reason"]
    assert m["R6"]["available"] is False
    assert body["zones"] == [] and body["queue"] == [] and body["warnings"] == []
    assert m["R1"]["detail"]["active"] == 0


@pytest.mark.db
async def test_ten_cases_are_enough_to_drop_the_small_sample_flag(api: ApiContext) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        for i in range(10):
            session.add(
                Rule(
                    repository_id=api.repository_id,
                    statement=f"r{i}",
                    confidence=0.9,
                    rationale="why",
                )
            )

    m = metrics(await get(api))

    assert m["R2"]["denominator"] == 10 and m["R2"]["small_sample"] is False


@pytest.mark.db
async def test_definitions_are_only_included_when_asked(api: ApiContext) -> None:
    plain = await get(api)
    explained = await get(api, explain="true")

    assert plain["definitions"] is None
    assert {"R2", "R3", "R3b", "R4", "R6", "V2", "zones", "queue"} <= set(explained["definitions"])
    for definition in explained["definitions"].values():
        assert definition["formula"] and definition["source"]


@pytest.mark.db
@pytest.mark.parametrize("window", [6, 366])
async def test_the_window_is_between_a_week_and_a_year(api: ApiContext, window: int) -> None:
    response = await api.client.get(url(api), params={"window": window}, headers=auth(api))

    assert response.status_code == 422


@pytest.mark.db
async def test_another_repositorys_token_and_anonymous_calls_see_nothing(api: ApiContext) -> None:
    await seed(api)
    other_token = generate_token()
    other_id = uuid.uuid4()
    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(
            Repository(
                id=other_id,
                github_id=9_200_000 + other_id.int % 1000,
                full_name="other/repo",
                installation_id=1,
                default_branch="main",
                api_token_hash=hash_token(other_token),
            )
        )
    try:
        mine_with_theirs = await api.client.get(
            url(api), headers={"Authorization": f"Bearer {other_token}"}
        )
        theirs_with_mine = await api.client.get(f"/v1/repos/{other_id}/health", headers=auth(api))
        anonymous = await api.client.get(url(api))
    finally:
        async with create_session_factory(api.engine)() as session, session.begin():
            await session.execute(text("delete from repositories where id = :id"), {"id": other_id})

    assert mine_with_theirs.status_code == 404
    assert theirs_with_mine.status_code == 404
    assert anonymous.status_code == 401


@pytest.mark.db
async def test_the_report_never_calls_the_model(api: ApiContext) -> None:
    await seed(api)

    class Forbidden:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"health must not use the model ({name})")

    api.client._transport.app.state.llm = Forbidden()  # type: ignore[attr-defined]

    body = await get(api, explain="true")

    assert body["knowledge"]


@pytest.mark.db
async def test_a_large_repository_answers_within_the_budget(api: ApiContext) -> None:
    """5 000 rules, 20 000 symbols (about 50 000 lines of code) and 20 000 calls: the report must stay
    well under the 2 s of the spec (3 s here to leave room for a slow CI machine)."""
    async with create_session_factory(api.engine)() as session, session.begin():
        await session.execute(
            text(
                """
                insert into symbols (id, repository_id, file_path, qualified_name, kind, body_hash,
                                     last_seen_sha, status)
                select gen_random_uuid(), :repo, 'src/zone' || (n % 40) || '/file' || (n % 400) || '.ts',
                       'fn' || n, 'function', 'h' || n, 's', 'active'
                from generate_series(1, 20000) n
                """
            ),
            {"repo": api.repository_id},
        )
        await session.execute(
            text(
                """
                insert into rules (id, repository_id, statement, confidence, rationale, revision,
                                   validated_revision, status)
                select gen_random_uuid(), :repo, 'rule ' || n, 0.9,
                       case when n % 3 = 0 then null else 'why' end, 1,
                       case when n % 5 = 0 then 1 else null end, 'active'
                from generate_series(1, 5000) n
                """
            ),
            {"repo": api.repository_id},
        )
        for table in ("symbols", "rules"):  # fresh bulk rows have no statistics yet
            await session.execute(text(f"analyze {table}"))
        await session.execute(
            text(
                """
                insert into rule_anchors (rule_id, symbol_id, role)
                select r.id, s.id, 'primary'
                from (select id, row_number() over () as n from rules where repository_id = :repo) r
                join (select id, row_number() over () as n from symbols where repository_id = :repo) s
                  on s.n = r.n
                """
            ),
            {"repo": api.repository_id},
        )
        await session.execute(
            text(
                """
                insert into symbol_calls (caller_id, callee_id)
                select distinct a.id, b.id
                from (select id, row_number() over () as n from symbols where repository_id = :repo) a
                join (select id, row_number() over () as n from symbols where repository_id = :repo) b
                  on b.n = ((a.n * 7) % 5000) + 1
                """
            ),
            {"repo": api.repository_id},
        )

        for table in ("rule_anchors", "symbol_calls"):
            await session.execute(text(f"analyze {table}"))

    started = time.perf_counter()
    body = await get(api, queue=50)
    elapsed = time.perf_counter() - started

    print(f"health report on 5000 rules: {elapsed:.2f}s")
    assert elapsed < 3.0, f"health took {elapsed:.2f}s"
    assert metrics(body)["R1"]["detail"]["active"] == 5000
    assert len(body["queue"]) == 50 and len(body["zones"]) > 10
