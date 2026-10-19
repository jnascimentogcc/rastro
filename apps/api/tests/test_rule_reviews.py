import uuid
from typing import Any

import pytest
from api_support import ApiContext
from sqlalchemy import select, update
from test_rules import DISCOUNT, IMPLANTS, auth, seed, statements, url

from rastro_db import create_session_factory
from rastro_db.models import Rule, RuleReview

pytestmark = pytest.mark.db

ANA = "ana@clinic.example"


def review(**body: Any) -> dict[str, Any]:
    return {"reviewer": ANA, **body}


async def post(api: ApiContext, rule_id: uuid.UUID, **body: Any) -> Any:
    return await api.client.post(
        url(api, f"/{rule_id}/reviews"), json=review(**body), headers=auth(api)
    )


async def detail(api: ApiContext, rule_id: uuid.UUID) -> dict[str, Any]:
    response = await api.client.get(url(api, f"/{rule_id}"), headers=auth(api))
    body: dict[str, Any] = response.json()
    return body


async def test_a_rule_starts_unvalidated(api: ApiContext) -> None:
    ids = await seed(api)

    body = await detail(api, ids["implants"])

    assert (body["validation"], body["revision"], body["validated_by"]) == ("unvalidated", 1, None)
    assert body["reviews"] == []


async def test_confirming_validates_the_current_revision_and_records_who(
    api: ApiContext,
) -> None:
    ids = await seed(api)

    response = await post(api, ids["implants"], verdict="confirmed", note="checked with Dr. Mendes")

    assert response.status_code == 200
    body = response.json()
    assert (body["validation"], body["validated_by"]) == ("validated", ANA)
    [entry] = body["reviews"]
    assert (entry["verdict"], entry["reviewer"], entry["reviewer_source"]) == (
        "confirmed",
        ANA,
        "cli",  # the name is not verified: anyone with the token could write another
    )
    assert entry["rule_revision"] == 1 and entry["note"] == "checked with Dr. Mendes"


async def test_the_validated_filter_separates_confirmed_from_pending_rules(
    api: ApiContext,
) -> None:
    ids = await seed(api)
    await post(api, ids["implants"], verdict="confirmed")

    yes = await api.client.get(url(api), params={"validated": "true"}, headers=auth(api))
    no = await api.client.get(url(api), params={"validated": "false"}, headers=auth(api))

    assert statements(yes) == [IMPLANTS]
    assert statements(no) == [DISCOUNT]


async def test_correcting_rewrites_the_rule_and_the_corrector_stands_behind_it(
    api: ApiContext,
) -> None:
    ids = await seed(api)
    await post(api, ids["implants"], verdict="confirmed")

    response = await post(
        api,
        ids["implants"],
        reviewer="rui@clinic.example",
        verdict="corrected",
        statement="Implants cannot be booked on Friday after noon",
    )

    body = response.json()
    assert body["statement"] == "Implants cannot be booked on Friday after noon"
    assert body["revision"] == 2
    assert (body["validation"], body["validated_by"]) == ("validated", "rui@clinic.example")
    assert [r["verdict"] for r in body["reviews"]] == ["corrected", "confirmed"]  # newest first
    assert body["reviews"][0]["statement_after"] == body["statement"]


async def test_adding_a_reason_marks_it_as_human_and_voids_an_earlier_confirmation(
    api: ApiContext,
) -> None:
    ids = await seed(api)
    await post(api, ids["discount"], verdict="confirmed")

    body = (
        await post(
            api, ids["discount"], verdict="rationale_added", rationale="Agreed with the union"
        )
    ).json()

    assert (body["rationale"], body["rationale_source"]) == ("Agreed with the union", "human")
    assert body["revision"] == 2
    assert body["validation"] == "unvalidated"  # the confirmation was of revision 1


async def test_rejecting_hides_the_rule_but_keeps_it_as_memory(api: ApiContext) -> None:
    ids = await seed(api)

    response = await post(api, ids["discount"], verdict="rejected", note="not a real rule")

    assert response.json()["validation"] == "rejected"
    default = await api.client.get(url(api), headers=auth(api))
    rejected = await api.client.get(url(api), params={"status": "rejected"}, headers=auth(api))
    unvalidated = await api.client.get(url(api), params={"validated": "false"}, headers=auth(api))
    assert statements(default) == [IMPLANTS]
    assert statements(rejected) == [DISCOUNT]
    assert DISCOUNT not in statements(unvalidated)  # a rejected rule is not "pending"
    async with create_session_factory(api.engine)() as session:
        rule = await session.get(Rule, ids["discount"])
        assert rule is not None and rule.status == "rejected" and rule.embedding is not None


async def test_a_rejected_rule_cannot_be_confirmed_but_can_be_rejected_again(
    api: ApiContext,
) -> None:
    ids = await seed(api)
    await post(api, ids["discount"], verdict="rejected")

    confirm = await post(api, ids["discount"], verdict="confirmed")
    again = await post(api, ids["discount"], verdict="rejected")

    assert confirm.status_code == 409 and again.status_code == 200


async def test_a_superseded_rule_cannot_be_reviewed(api: ApiContext) -> None:
    ids = await seed(api)
    async with create_session_factory(api.engine)() as session, session.begin():
        await session.execute(
            update(Rule).where(Rule.id == ids["discount"]).values(status="superseded")
        )

    assert (await post(api, ids["discount"], verdict="confirmed")).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"verdict": "corrected"},  # needs the new statement
        {"verdict": "corrected", "statement": "   "},
        {"verdict": "rationale_added"},  # needs the reason
        {"verdict": "confirmed", "reviewer": "  "},
        {"verdict": "confirmed", "note": "x" * 501},
        {"verdict": "approved"},
    ],
)
async def test_invalid_reviews_are_rejected(api: ApiContext, body: dict[str, Any]) -> None:
    ids = await seed(api)

    response = await api.client.post(
        url(api, f"/{ids['implants']}/reviews"), json=review(**body), headers=auth(api)
    )

    assert response.status_code == 422
    async with create_session_factory(api.engine)() as session:
        assert (await session.scalars(select(RuleReview))).all() == []


async def test_reviews_are_limited_to_the_own_repository_and_need_the_token(
    api: ApiContext,
) -> None:
    ids = await seed(api)

    foreign = await api.client.post(
        f"/v1/repos/{uuid.uuid4()}/rules/{ids['implants']}/reviews",
        json=review(verdict="confirmed"),
        headers=auth(api),
    )
    unknown = await post(api, uuid.uuid4(), verdict="confirmed")
    anonymous = await api.client.post(
        url(api, f"/{ids['implants']}/reviews"), json=review(verdict="confirmed")
    )

    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert anonymous.status_code in (401, 403)
