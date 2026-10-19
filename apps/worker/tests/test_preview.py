"""Phase 9.2: the rules a pull request would teach Rastro, shown before the merge."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from test_budget import spend
from test_merged_pr import NUMBER, stage_pull_request
from test_rule_extraction import AFTER, AGENDA, BEFORE, KEY, SHA1, SHA2, SHA3, rule
from worker_support import WorkerContext, user_excerpt

from rastro_db.models import Repository, Rule, RuleProposal
from rastro_llm import LLMQuotaError, RuleExtraction, hashing_embedding
from rastro_worker.comment import MARKER
from rastro_worker.impact import analyze_pull_request
from rastro_worker.indexing import index_repository
from rastro_worker.preview import proposal_key, refresh_preview

FRIDAY = "Implants cannot be booked on Friday afternoon"
SATURDAY = "Implants cannot be booked on Friday afternoon or on Saturday"


async def preview(worker: WorkerContext):  # type: ignore[no-untyped-def]
    async with worker.deps.session_factory() as session:
        repository = await session.get(Repository, worker.repository_id)
    assert repository is not None and worker.deps.llm is not None
    return await refresh_preview(worker.deps, worker.deps.llm, repository, NUMBER)


async def stored(worker: WorkerContext) -> list[RuleProposal]:
    async with worker.deps.session_factory() as session:
        return list(
            await session.scalars(
                select(RuleProposal)
                .where(RuleProposal.repository_id == worker.repository_id)
                .order_by(RuleProposal.commit_sha)
            )
        )


def friday(**overrides: object):  # type: ignore[no-untyped-def]
    return rule(statement=FRIDAY, **overrides)


async def test_a_pull_request_shows_the_rules_it_proposes_without_touching_the_graph(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[]))

    outcome = await preview(worker)

    [item] = outcome.items
    assert (item.statement, item.action, item.symbol) == (FRIDAY, "create", KEY)
    assert item.rationale is None  # the session never said why, so the reason is not invented
    assert outcome.previewed == 2 and outcome.unavailable is None
    assert await worker.rules() == []  # nothing reached the rules...
    assert await worker.symbols() == {}  # ...and not even a symbol was written
    [row] = await stored(worker)
    assert (row.commit_sha, row.state, row.pr_number) == (SHA2, "pending", NUMBER)


async def test_commits_without_a_session_are_not_sent_to_the_model(worker: WorkerContext) -> None:
    await stage_pull_request(worker)  # SHA1 is a chore commit with no session
    worker.llm.queue(RuleExtraction(rules=[]))
    worker.llm.queue(RuleExtraction(rules=[]))

    await preview(worker)

    assert len(worker.llm.calls) == 2


async def test_a_commit_is_previewed_once_and_a_late_session_runs_it_again(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[]))
    await preview(worker)
    calls = len(worker.llm.calls)

    again = await preview(worker)  # a new push of the PR, nothing new in it

    assert len(worker.llm.calls) == calls and again.previewed == 0
    assert [i.statement for i in again.items] == [FRIDAY]  # still shown, at no cost

    await worker.add_session(SHA3, [user_excerpt("Also block Saturday for implants.")])
    worker.llm.queue(RuleExtraction(rules=[rule(statement=SATURDAY)]))
    late = await preview(worker)

    assert len(worker.llm.calls) == calls + 1 and late.previewed == 1  # only that commit


async def test_a_later_commit_replaces_the_earlier_proposal_on_the_same_symbol(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[rule(statement=SATURDAY)]))

    outcome = await preview(worker)

    assert [i.statement for i in outcome.items] == [SATURDAY]  # last commit wins
    states = {r.commit_sha: r.state for r in await stored(worker)}
    assert states == {SHA2: "superseded", SHA3: "pending"}


async def test_proposals_of_commits_that_left_the_pull_request_are_dropped(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[rule(statement="Another rule", symbols=[KEY])]))
    await preview(worker)

    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [SHA1, SHA3])  # force push
    outcome = await preview(worker)

    assert [i.statement for i in outcome.items] == ["Another rule"]
    assert [r.commit_sha for r in await stored(worker)] == [SHA3]


async def test_an_update_shows_the_rule_it_changes_and_who_confirmed_it(
    worker: WorkerContext,
) -> None:
    worker.github.set_tree(worker.full_name, SHA1, {AGENDA: BEFORE}, head_of="main")
    await index_repository(worker.deps, worker.repository_id)
    existing = await worker.add_rule(
        FRIDAY,
        {(AGENDA, "AgendaService.validate"): "primary"},
        rationale="The doctor needs 48 hours of follow-up",
    )
    when = datetime(2026, 10, 12, tzinfo=UTC)
    async with worker.deps.session_factory() as session, session.begin():
        await session.execute(
            update(Rule)
            .where(Rule.id == existing)
            .values(validated_revision=1, validated_by="@ana", validated_at=when)
        )
    await stage_pull_request(worker)
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(
                    action="update",
                    existing_rule_id=str(existing),
                    statement=SATURDAY,
                    rationale=None,
                    origin_quote=None,
                )
            ]
        )
    )
    worker.llm.queue(RuleExtraction(rules=[]))

    [item] = (await preview(worker)).items

    assert (item.action, item.replaces, item.statement) == ("update", FRIDAY, SATURDAY)
    assert (item.validated_by, item.validated_at) == ("@ana", when)
    assert (await worker.rules())[0].statement == FRIDAY  # the stored rule is untouched


async def test_a_rule_a_human_rejected_is_not_proposed_again(worker: WorkerContext) -> None:
    worker.github.set_tree(worker.full_name, SHA1, {AGENDA: BEFORE}, head_of="main")
    await index_repository(worker.deps, worker.repository_id)
    await worker.add_rule(
        FRIDAY,
        {(AGENDA, "AgendaService.validate"): "primary"},
        status="rejected",
        embedding=hashing_embedding(FRIDAY, 1024),
    )
    await stage_pull_request(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[]))

    assert (await preview(worker)).items == []


async def test_without_budget_nothing_is_sent_and_the_outcome_says_so(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.deps.monthly_token_budget = 1_000
    await spend(worker, 1_000)

    outcome = await preview(worker)

    assert outcome.unavailable == "budget" and outcome.items == []
    assert worker.llm.calls == []


async def test_an_empty_balance_is_reported_not_hidden(worker: WorkerContext) -> None:
    await stage_pull_request(worker)
    worker.llm.queue(LLMQuotaError("credit_balance_exhausted"))

    outcome = await preview(worker)

    assert outcome.unavailable == "quota" and outcome.items == []
    assert await stored(worker) == []  # not cached: it runs again once there is credit


async def test_a_model_failure_is_not_cached(worker: WorkerContext) -> None:
    await stage_pull_request(worker)  # nothing queued: the fake model fails

    first = await preview(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[]))
    second = await preview(worker)

    assert first.items == [] and first.previewed == 0
    assert [i.statement for i in second.items] == [FRIDAY]


def test_the_same_rule_on_the_same_symbols_has_a_stable_key() -> None:
    a = proposal_key("Implants cannot  be booked", ["b.ts::f", "a.ts::g"])
    b = proposal_key("implants cannot be booked", ["a.ts::g", "b.ts::f"])

    assert a == b and a != proposal_key("Implants cannot be booked", ["a.ts::g"])


# ----------------------------------------------------------------- inside the pull request analysis


async def stage_analysis(worker: WorkerContext) -> None:
    await stage_pull_request(worker)
    worker.stage_pull_request(NUMBER, {AGENDA: AFTER}, {AGENDA: BEFORE})
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[]))


async def analyze(worker: WorkerContext):  # type: ignore[no-untyped-def]
    return await analyze_pull_request(worker.deps, worker.repository_id, NUMBER, "a" * 40)


async def test_the_pull_request_comment_lists_the_proposals_and_is_edited_in_place(
    worker: WorkerContext,
) -> None:
    await stage_analysis(worker)

    first = await analyze(worker)
    calls = len(worker.llm.calls)
    second = await analyze(worker)  # the next push: the preview is cached

    assert (first.proposals, first.commented, first.reported) == (1, True, 0)
    [body] = worker.github.comments_on(NUMBER)
    assert body.startswith(MARKER) and f"- [ ] **{FRIDAY}**" in body
    assert "rules affected" not in body  # no impact finding, only the proposals
    assert second.comment_id == first.comment_id and len(worker.llm.calls) == calls


async def test_the_comment_becomes_neutral_when_nothing_is_proposed_any_more(
    worker: WorkerContext,
) -> None:
    await stage_analysis(worker)
    await analyze(worker)

    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [SHA1])  # the commits left
    result = await analyze(worker)

    assert result.proposals == 0 and result.commented is True
    [body] = worker.github.comments_on(NUMBER)
    assert "none is proposed" in body and FRIDAY not in body


async def test_a_pull_request_with_nothing_to_say_still_gets_no_comment(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.stage_pull_request(NUMBER, {AGENDA: AFTER}, {AGENDA: BEFORE})
    worker.llm.queue(RuleExtraction(rules=[]))
    worker.llm.queue(RuleExtraction(rules=[]))

    result = await analyze(worker)

    assert (result.proposals, result.commented) == (0, False)
    assert worker.github.comments == {}


async def test_over_budget_keeps_showing_cached_proposals_and_says_the_rest_is_missing(
    worker: WorkerContext,
) -> None:
    await stage_analysis(worker)
    await analyze(worker)
    await worker.add_session(SHA3, [user_excerpt("A newer session for the last commit.")])
    worker.deps.monthly_token_budget = 1_000
    await spend(worker, 1_000)

    result = await analyze(worker)

    assert result.status == "skipped_budget" and result.commented is True
    [body] = worker.github.comments_on(NUMBER)
    assert f"- [ ] **{FRIDAY}**" in body  # what was already previewed stays visible
    assert "monthly cost limit was reached" in body  # and the gap is not silent


async def test_over_budget_without_sessions_to_preview_posts_nothing(
    worker: WorkerContext,
) -> None:
    worker.stage_pull_request(NUMBER, {AGENDA: AFTER}, {AGENDA: BEFORE})
    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [SHA1])
    worker.deps.monthly_token_budget = 1_000
    await spend(worker, 1_000)

    result = await analyze(worker)

    assert result.status == "skipped_budget" and worker.github.comments == {}


async def test_a_failing_preview_does_not_cost_the_pull_request_its_comment(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.stage_pull_request(NUMBER, {AGENDA: AFTER}, {AGENDA: BEFORE})
    ghost = "9" * 40  # a PR commit GitHub can no longer show (its refs are gone)
    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [ghost])
    await worker.add_session(ghost, [user_excerpt("Something nobody can read any more.")])

    result = await analyze(worker)

    assert result.status == "analyzed" and result.proposals == 0  # no crash, no stale data
