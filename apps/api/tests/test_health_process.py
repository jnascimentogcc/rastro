"""Phase 11.2: R3a, the process figures (P1-P5) and the source of intent (V1)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_support import ApiContext

from rastro_db import create_session_factory
from rastro_db.models import AgentSession, Commit, Rule, RuleAnchor, RuleProposal, RuleReview
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


def by_id(metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {metric["id"]: metric for metric in metrics}


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


# --------------------------------------------------------------------------- R3a


async def test_r3a_counts_confirmations_made_by_someone_other_than_the_pr_author(
    api: ApiContext,
) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        symbol = SymbolRow(
            repository_id=api.repository_id,
            file_path="src/a.ts",
            qualified_name="f",
            kind="function",
            body_hash="h",
            last_seen_sha="s",
            status="active",
        )
        session.add(symbol)
        for name, source, is_author in [
            ("other", "github", False),  # another login confirmed it
            ("author", "github", True),  # the PR author confirmed their own rule
            ("cli", "cli", None),  # the CLI name is unverified
            ("none", None, None),  # confirmed, but no review row survives
            ("old", "github", None),  # a GitHub review from before the PR author was recorded
        ]:
            rule = Rule(
                repository_id=api.repository_id,
                statement=f"rule {name}",
                confidence=0.9,
                revision=1,
                validated_revision=1,
            )
            session.add(rule)
            await session.flush()
            session.add(RuleAnchor(rule_id=rule.id, symbol_id=symbol.id, role="primary"))
            if source:
                session.add(
                    RuleReview(
                        rule_id=rule.id,
                        rule_revision=1,
                        verdict="confirmed",
                        reviewer="ana",
                        reviewer_source=source,
                        reviewer_is_pr_author=is_author,
                    )
                )

    r3a = by_id((await get(api))["knowledge"])["R3a"]

    assert (r3a["numerator"], r3a["denominator"]) == (1, 2)  # one of the two with both logins known
    assert r3a["detail"] == {"unknown": 3}  # CLI, no review and the older GitHub review


async def test_r3a_is_not_available_when_no_confirmation_has_a_known_pr_author(
    api: ApiContext,
) -> None:
    r3a = by_id((await get(api))["knowledge"])["R3a"]

    assert (
        r3a["available"] is False and r3a["na_reason"] == "no confirmation with a known PR author"
    )


# --------------------------------------------------------------------------- P1-P5


async def seed_proposals(api: ApiContext, reviewers: tuple[str, ...]) -> None:
    """PR 10: p1 applied and reviewed, p2 applied but never reviewed. PR 11: p3 applied and reviewed.
    PR 12: p4 still pending. PR 9: p5, applied long before the 30-day window."""
    async with create_session_factory(api.engine)() as session, session.begin():
        ana, bo, cy = (reviewers + ("ana", "bo", "cy"))[:3]
        for i, (pr, state, created, reviewed, by) in enumerate(
            [
                (10, "applied", ago(days=3), ago(days=2), ana),  # reviewed after 24 h
                (10, "applied", ago(days=3), None, None),
                (11, "applied", ago(hours=10), ago(hours=2), bo),  # reviewed after 8 h
                (12, "pending", ago(days=1), None, None),
                (9, "applied", ago(days=100), ago(days=99), ana),
                (13, "applied", ago(hours=6), ago(hours=3), cy),  # reviewed after 3 h
            ]
        ):
            session.add(
                RuleProposal(
                    repository_id=api.repository_id,
                    pr_number=pr,
                    commit_sha=f"{i}" * 40,
                    proposal_key=f"k{i}",
                    action="create",
                    statement=f"proposal {i}",
                    symbols=[],
                    confidence=0.9,
                    state=state,
                    created_at=created,
                    reviewed_at=reviewed,
                    reviewed_by=by,
                )
            )


async def test_process_figures_count_what_happened_in_the_window(api: ApiContext) -> None:
    await seed_proposals(api, ("ana", "bo", "cy"))

    process = by_id((await get(api))["process"])

    assert process["P1"]["numerator"] == 4  # PRs 10, 11, 12, 13; PR 9 is 100 days old
    assert (process["P2"]["numerator"], process["P2"]["denominator"]) == (3, 4)
    assert (process["P3"]["numerator"], process["P3"]["denominator"]) == (
        1,
        4,
    )  # p2 slipped through
    assert process["P4"]["detail"] == {"median_hours": 8.0}  # 24 h, 8 h and 3 h
    assert process["P4"]["numerator"] == 3


async def test_a_wider_window_brings_older_proposals_in(api: ApiContext) -> None:
    await seed_proposals(api, ("ana", "bo", "cy"))

    process = by_id((await get(api, window=120))["process"])

    assert process["P1"]["numerator"] == 5
    assert (process["P2"]["numerator"], process["P2"]["denominator"]) == (4, 5)


async def test_review_timing_is_withheld_when_fewer_than_three_people_reviewed(
    api: ApiContext,
) -> None:
    await seed_proposals(api, ("ana", "ana", "bo"))  # only two distinct reviewers

    p4 = by_id((await get(api))["process"])["P4"]

    assert p4["available"] is False and "fewer than 3 distinct reviewers" in p4["na_reason"]
    # the counts that name nobody are still shown
    assert by_id((await get(api))["process"])["P3"]["available"] is True


async def seed_verdicts(api: ApiContext, reviewers: list[str]) -> None:
    async with create_session_factory(api.engine)() as session, session.begin():
        rule = Rule(repository_id=api.repository_id, statement="r", confidence=0.9)
        session.add(rule)
        await session.flush()
        verdicts = ["confirmed", "corrected", "rejected", "confirmed", "rationale_added"]
        for verdict, reviewer in zip(verdicts, reviewers, strict=True):
            session.add(
                RuleReview(
                    rule_id=rule.id,
                    rule_revision=1,
                    verdict=verdict,
                    reviewer=reviewer,
                    reviewer_source="github",
                    created_at=ago(days=1),
                )
            )


async def test_p5_splits_the_verdicts_and_ignores_added_reasons(api: ApiContext) -> None:
    await seed_verdicts(api, ["ana", "bo", "cy", "ana", "bo"])

    p5 = by_id((await get(api))["process"])["P5"]

    assert (p5["numerator"], p5["denominator"]) == (2, 4)  # rationale_added is not a verdict
    assert p5["detail"] == {"confirmed": 2, "corrected": 1, "rejected": 1}


async def test_p5_is_withheld_when_it_could_point_at_a_person(api: ApiContext) -> None:
    await seed_verdicts(api, ["ana", "ana", "bo", "bo", "ana"])

    p5 = by_id((await get(api))["process"])["P5"]

    assert p5["available"] is False and "fewer than 3 distinct reviewers" in p5["na_reason"]


async def test_an_empty_window_gives_not_available_for_every_ratio(api: ApiContext) -> None:
    body = await get(api)
    process = by_id(body["process"])

    assert process["P1"]["numerator"] == 0  # a count, and zero is true
    for key in ("P2", "P3"):
        assert process[key]["available"] is False and process[key]["value"] is None
    assert process["P5"]["available"] is False
    assert by_id(body["intent"])["V1"]["available"] is False


# --------------------------------------------------------------------------- V1


async def seed_commits(api: ApiContext) -> None:
    """Units: PR 10 (a commit with a session, one without, and the merge commit): has intent.
    PR 11 (no session anywhere): none. PR 20, too old for the window. PR 30: its commit with a session
    is 45 days old but the merge commit is recent, so the unit is dated by its newest commit.
    d1 is a direct commit with a session, d2 a direct commit without one (unattributed). f1 is on a
    feature branch and does not count."""
    rows = [
        ("c1", 10, ago(days=20), True, True),
        ("c2", 10, ago(days=20), False, True),
        ("m10", 10, ago(days=19), False, True),
        ("c3", 11, ago(days=5), False, True),
        ("m11", 11, ago(days=4), False, True),
        ("c4", 20, ago(days=90), True, True),
        ("c5", 30, ago(days=45), True, True),
        ("m30", 30, ago(days=5), False, True),
        ("d1", None, ago(days=2), True, True),
        ("d2", None, ago(days=3), False, True),
        ("f1", None, ago(days=1), True, False),
    ]
    async with create_session_factory(api.engine)() as session, session.begin():
        for name, pr, committed, with_session, default in rows:
            sha = (name.ljust(40, "0"))[:40]
            session.add(
                Commit(
                    repository_id=api.repository_id,
                    sha=sha,
                    committed_at=committed,
                    on_default_branch=default,
                    merged_pr_number=pr,
                )
            )
            if with_session:
                session.add(
                    AgentSession(
                        repository_id=api.repository_id,
                        commit_sha=sha,
                        session_ref=name,
                        excerpts=[],
                    )
                )


async def test_v1_counts_pull_requests_and_direct_commits_without_guessing(api: ApiContext) -> None:
    await seed_commits(api)

    v1 = by_id((await get(api))["intent"])["V1"]

    # pull requests 10, 11 and 30 are in the window; 10 and 30 have a session; d1 is a direct commit
    # with intent. PR 11 is the only integration with no recorded intent.
    assert (v1["numerator"], v1["denominator"]) == (1, 4)
    assert v1["detail"] == {
        "pull_requests": 3,
        "pull_requests_with_session": 2,
        "direct_commits_with_session": 1,
        "unattributed_commits": 1,  # d2: cannot be told apart from an older merge commit
        "undated_units": 0,
    }
    assert v1["small_sample"] is True


async def test_v1_counts_a_squash_commit_with_the_pull_request_whose_sessions_it_hides(
    api: ApiContext,
) -> None:
    await seed_commits(api)

    wider = by_id((await get(api, window=120))["intent"])["V1"]

    assert wider["detail"]["pull_requests"] == 4  # PR 20 comes in with its session
    assert (wider["numerator"], wider["denominator"]) == (1, 5)
