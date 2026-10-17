import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import ApiContext

from rastro_db import create_session_factory, generate_token, hash_token
from rastro_db.models import Commit, Repository, Rule, RuleAnchor, RuleOrigin
from rastro_db.models import Symbol as SymbolRow
from rastro_llm import FakeProvider, LLMError, hashing_embedding

pytestmark = pytest.mark.db

IMPLANTS = "Implants cannot be booked on Friday afternoon"
DISCOUNT = "Patients aged 65 or older get a 20% discount"
OLD = "Reminders go out three days before"
SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
WHEN = datetime(2026, 3, 12, 9, 30, tzinfo=UTC)


def auth(api: ApiContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {api.token}"}


def url(api: ApiContext, suffix: str = "") -> str:
    return f"/v1/repos/{api.repository_id}/rules{suffix}"


def llm_of(api: ApiContext) -> Any:
    return api.client._transport.app.state  # type: ignore[attr-defined]


async def seed(api: ApiContext) -> dict[str, uuid.UUID]:
    """Three rules: an active one with a duplicate anchor and an origin, an active one with no
    stated reason, and an orphaned one whose symbol is gone."""
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
                end_line=9,
                last_seen_sha="s",
                status=status,
            )
            session.add(row)
            return row

        agenda = symbol("src/agenda.ts", "validate")
        calendar = symbol("src/calendar.ts", "isAllowed")
        pricing = symbol("src/pricing.ts", "priceFor")
        gone = symbol("src/old.ts", "remind", "removed")
        implants = Rule(
            repository_id=api.repository_id,
            statement=IMPLANTS,
            rationale="Dr. Mendes needs 48h of follow-up",
            confidence=0.9,
            embedding=hashing_embedding(IMPLANTS, 1024),
            updated_at=WHEN + timedelta(days=2),
        )
        discount = Rule(
            repository_id=api.repository_id,
            statement=DISCOUNT,
            rationale=None,
            confidence=0.8,
            embedding=hashing_embedding(DISCOUNT, 1024),
            updated_at=WHEN + timedelta(days=3),
        )
        old = Rule(
            repository_id=api.repository_id,
            statement=OLD,
            confidence=0.7,
            status="orphaned",
            embedding=hashing_embedding(OLD, 1024),
            updated_at=WHEN,
        )
        session.add_all([implants, discount, old])
        session.add(Commit(repository_id=api.repository_id, sha=SHA, committed_at=WHEN))
        await session.flush()
        session.add_all(
            [
                RuleAnchor(rule_id=implants.id, symbol_id=agenda.id, role="primary"),
                RuleAnchor(rule_id=implants.id, symbol_id=calendar.id, role="duplicate"),
                RuleAnchor(rule_id=discount.id, symbol_id=pricing.id, role="primary"),
                RuleAnchor(rule_id=old.id, symbol_id=gone.id, role="primary"),
                RuleOrigin(
                    rule_id=implants.id, commit_sha=SHA, quote="Dr. Mendes needs 48h of follow-up"
                ),
            ]
        )
        ids.update(implants=implants.id, discount=discount.id, old=old.id)
    return ids


def statements(response: Any) -> list[str]:
    return [item["statement"] for item in response.json()["items"]]


# --------------------------------------------------------------------------- listing


async def test_lists_active_rules_newest_first_with_files_and_origin(api: ApiContext) -> None:
    await seed(api)

    response = await api.client.get(url(api), headers=auth(api))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2 and body["search"] is None
    assert statements(response) == [DISCOUNT, IMPLANTS]  # the orphaned one is not shown
    discount, implants = body["items"]
    assert discount["rationale"] is None  # unknown origin stays null
    assert implants["files"] == ["src/agenda.ts", "src/calendar.ts"]
    assert implants["origin"]["commit_sha"] == SHA
    assert implants["origin"]["quote"] == "Dr. Mendes needs 48h of follow-up"
    assert implants["origin"]["committed_at"].startswith("2026-03-12T09:30")
    assert discount["origin"] is None


async def test_status_filter(api: ApiContext) -> None:
    await seed(api)

    orphaned = await api.client.get(url(api), params={"status": "orphaned"}, headers=auth(api))
    everything = await api.client.get(url(api), params={"status": "all"}, headers=auth(api))

    assert statements(orphaned) == [OLD]
    assert len(statements(everything)) == 3
    assert (
        await api.client.get(url(api), params={"status": "bogus"}, headers=auth(api))
    ).status_code == 422


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/agenda.ts", [IMPLANTS]),
        ("src\\agenda.ts", [IMPLANTS]),  # Windows separators from the CLI
        ("src/calendar.ts", [IMPLANTS]),  # also reachable through the duplicate anchor
        ("src", [DISCOUNT, IMPLANTS]),  # a directory
        ("src/", [DISCOUNT, IMPLANTS]),
        ("src/agenda", []),  # no partial file names
        ("elsewhere/file.ts", []),
    ],
)
async def test_file_filter(api: ApiContext, path: str, expected: list[str]) -> None:
    await seed(api)

    response = await api.client.get(url(api), params={"file": path}, headers=auth(api))

    assert statements(response) == expected


async def test_orphaned_rules_can_be_found_by_their_old_file(api: ApiContext) -> None:
    await seed(api)

    response = await api.client.get(
        url(api), params={"file": "src/old.ts", "status": "orphaned"}, headers=auth(api)
    )

    assert statements(response) == [OLD]


async def test_pagination_keeps_the_total(api: ApiContext) -> None:
    await seed(api)

    page = await api.client.get(url(api), params={"limit": 1, "offset": 1}, headers=auth(api))

    assert statements(page) == [IMPLANTS] and page.json()["total"] == 2
    assert (
        await api.client.get(url(api), params={"limit": 0}, headers=auth(api))
    ).status_code == 422


# --------------------------------------------------------------------------- search


async def test_semantic_search_ranks_by_similarity_and_drops_noise(api: ApiContext) -> None:
    await seed(api)

    response = await api.client.get(
        url(api), params={"q": "can implants be booked on friday"}, headers=auth(api)
    )

    body = response.json()
    assert body["search"] == "semantic"
    assert statements(response) == [IMPLANTS]  # the discount rule is below the cutoff
    assert body["items"][0]["similarity"] > 0.3


async def test_search_combines_with_the_file_filter(api: ApiContext) -> None:
    await seed(api)

    response = await api.client.get(
        url(api), params={"q": "implants friday", "file": "src/pricing.ts"}, headers=auth(api)
    )

    assert statements(response) == []


@pytest.mark.parametrize("broken", ["no_model", "model_error"])
async def test_search_falls_back_to_text_without_a_working_model(
    api: ApiContext, broken: str
) -> None:
    await seed(api)

    class Failing(FakeProvider):
        async def embed(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
            raise LLMError("embedding service down")

    llm_of(api).llm = None if broken == "no_model" else Failing()

    response = await api.client.get(url(api), params={"q": "DISCOUNT"}, headers=auth(api))

    assert response.json()["search"] == "text"
    assert statements(response) == [DISCOUNT]  # case-insensitive substring


async def test_text_search_also_looks_at_the_reason_and_treats_wildcards_literally(
    api: ApiContext,
) -> None:
    await seed(api)
    llm_of(api).llm = None

    by_reason = await api.client.get(url(api), params={"q": "follow-up"}, headers=auth(api))
    percent = await api.client.get(url(api), params={"q": "%"}, headers=auth(api))
    wildcard = await api.client.get(url(api), params={"q": "Impl%nts"}, headers=auth(api))
    underscore = await api.client.get(url(api), params={"q": "Impl_nts"}, headers=auth(api))

    assert statements(by_reason) == [IMPLANTS]
    assert statements(percent) == [DISCOUNT]  # a literal percent sign, not "match everything"
    assert statements(wildcard) == [] and statements(underscore) == []


async def test_a_blank_search_is_ignored(api: ApiContext) -> None:
    await seed(api)

    response = await api.client.get(url(api), params={"q": "   "}, headers=auth(api))

    assert response.json()["search"] is None and len(statements(response)) == 2


# --------------------------------------------------------------------------- detail & isolation


async def test_detail_includes_anchors_and_origins(api: ApiContext) -> None:
    ids = await seed(api)

    response = await api.client.get(url(api, f"/{ids['implants']}"), headers=auth(api))

    assert response.status_code == 200
    body = response.json()
    assert (
        body["statement"] == IMPLANTS and body["rationale"] == "Dr. Mendes needs 48h of follow-up"
    )
    assert [(a["file_path"], a["role"]) for a in body["anchors"]] == [
        ("src/agenda.ts", "primary"),
        ("src/calendar.ts", "duplicate"),
    ]
    assert (
        body["anchors"][0]["qualified_name"] == "validate"
        and body["anchors"][0]["status"] == "active"
    )
    assert [o["commit_sha"] for o in body["origins"]] == [SHA]


async def test_detail_of_an_orphaned_rule_shows_the_removed_anchor(api: ApiContext) -> None:
    ids = await seed(api)

    body = (await api.client.get(url(api, f"/{ids['old']}"), headers=auth(api))).json()

    assert body["status"] == "orphaned"
    assert [(a["file_path"], a["status"]) for a in body["anchors"]] == [("src/old.ts", "removed")]


async def test_unknown_rule_and_foreign_repository_are_not_found(api: ApiContext) -> None:
    ids = await seed(api)
    other_token = generate_token()
    other_id = uuid.uuid4()
    async with create_session_factory(api.engine)() as session, session.begin():
        session.add(
            Repository(
                id=other_id,
                github_id=9_100_500 + other_id.int % 1000,
                full_name="other/repo",
                installation_id=1,
                default_branch="main",
                api_token_hash=hash_token(other_token),
            )
        )

    unknown = await api.client.get(url(api, f"/{uuid.uuid4()}"), headers=auth(api))
    other_repo_url = f"/v1/repos/{other_id}/rules"
    foreign = await api.client.get(other_repo_url, headers=auth(api))
    foreign_detail = await api.client.get(f"{other_repo_url}/{ids['implants']}", headers=auth(api))
    theirs = await api.client.get(
        url(api, f"/{ids['implants']}"), headers={"Authorization": f"Bearer {other_token}"}
    )

    assert unknown.status_code == 404
    assert foreign.status_code == 404 and foreign_detail.status_code == 404
    assert theirs.status_code == 404  # our rule is invisible to another repository's token
    async with create_session_factory(api.engine)() as session, session.begin():
        await session.execute(
            Repository.__table__.delete().where(Repository.id == other_id)  # type: ignore[attr-defined]
        )


async def test_rules_require_the_repository_token(api: ApiContext) -> None:
    for headers in ({}, {"Authorization": "Bearer rst_wrong"}):
        response = await api.client.get(url(api), headers=headers)
        assert response.status_code == 401
    assert (await api.client.get(url(api, f"/{uuid.uuid4()}"))).status_code == 401
    assert (
        await api.client.get("/v1/repos/not-a-uuid/rules", headers=auth(api))
    ).status_code == 422
