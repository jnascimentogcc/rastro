"""Phase 9.4: when the pull request is merged, the stored proposals (with the reviews people made
in GitHub) become rules, without asking the model again."""

import uuid

from sqlalchemy import select, update
from test_impact import verdict
from test_merged_pr import NUMBER, stage_pull_request
from test_preview import FRIDAY, SATURDAY, analyze, friday
from test_review import ANA, previewed, review
from test_rule_extraction import AFTER, AFTER_SATURDAY, AGENDA, BEFORE, SHA2, SHA3, rule
from worker_support import WorkerContext, user_excerpt

from rastro_db.models import Commit, Rule, RuleProposal, RuleReview
from rastro_llm import LLMQuotaError, RuleExtraction
from rastro_worker.indexing import index_repository
from rastro_worker.preview import set_pr_author
from rastro_worker.rule_extraction import extract_rules

SHA4 = "6" * 40


async def merge(worker: WorkerContext, *, force: bool = False):  # type: ignore[no-untyped-def]
    """What the merge does: the PR's commits become default-branch commits and are extracted."""
    summaries = []
    for sha in (SHA2, SHA3):
        await worker.set_on_default_branch(sha, True)
        summaries.append(await extract_rules(worker.deps, worker.repository_id, sha, force=force))
    return summaries


async def all_rules(worker: WorkerContext) -> list[Rule]:
    return await worker.rules()


async def reviews(worker: WorkerContext) -> list[RuleReview]:
    async with worker.deps.session_factory() as session:
        return list(
            await session.scalars(
                select(RuleReview)
                .join(Rule, Rule.id == RuleReview.rule_id)
                .where(Rule.repository_id == worker.repository_id)
                .order_by(RuleReview.created_at, RuleReview.verdict)
            )
        )


async def proposal_states(worker: WorkerContext) -> dict[str, str]:
    async with worker.deps.session_factory() as session:
        rows = await session.scalars(
            select(RuleProposal).where(RuleProposal.repository_id == worker.repository_id)
        )
        return {r.commit_sha: r.state for r in rows}


async def test_a_confirmed_proposal_becomes_a_validated_rule_without_asking_the_model(
    worker: WorkerContext,
) -> None:
    _, _, key = await previewed(worker)
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")
    calls = len(worker.llm.calls)

    first, second = await merge(worker)

    assert len(worker.llm.calls) == calls  # no extraction call: the preview is what lands
    assert (first.from_preview, first.created, first.validated) == (True, 1, 1)
    assert second.from_preview and second.created == 0
    [created] = await all_rules(worker)
    assert (created.statement, created.status) == (FRIDAY, "active")
    assert (created.validated_revision, created.revision) == (1, 1)
    assert created.validated_by == ANA and created.validated_at is not None
    [entry] = await reviews(worker)
    assert (entry.verdict, entry.reviewer, entry.reviewer_source) == ("confirmed", ANA, "github")
    assert entry.rule_revision == 1 and entry.pr_number == NUMBER
    assert (await proposal_states(worker))[SHA2] == "applied"
    assert (await worker.commit(SHA2)).analysis_status == "analyzed"  # type: ignore[union-attr]


async def test_a_correction_is_the_text_that_lands_and_counts_as_validated(
    worker: WorkerContext,
) -> None:
    _, _, key = await previewed(worker)
    await review(worker, 1001, body=f"/rastro correct {key[:6]}: Implants wait until Monday")

    await merge(worker)

    [created] = await all_rules(worker)
    assert created.statement == "Implants wait until Monday"
    assert (created.validated_by, created.validated_revision) == (ANA, created.revision)
    [entry] = await reviews(worker)
    assert (entry.verdict, entry.statement_after) == ("corrected", "Implants wait until Monday")


async def test_an_unreviewed_proposal_still_lands_but_is_not_validated(
    worker: WorkerContext,
) -> None:
    await previewed(worker)

    await merge(worker)

    [created] = await all_rules(worker)
    assert created.statement == FRIDAY and created.validated_revision is None
    assert await reviews(worker) == []


async def test_a_reason_written_by_a_person_is_kept_and_attributed(worker: WorkerContext) -> None:
    _, _, key = await previewed(worker)
    await review(worker, 1001, body=f"/rastro why {key[:6]} The doctor needs 48h of follow-up")

    await merge(worker)

    [created] = await all_rules(worker)
    assert created.rationale == "The doctor needs 48h of follow-up"  # not in the conversation
    assert (created.rationale_source, created.validated_revision) == ("human", None)
    [entry] = await reviews(worker)
    assert (entry.verdict, entry.reviewer, entry.rationale_after) == (
        "rationale_added",
        ANA,
        "The doctor needs 48h of follow-up",
    )


async def test_a_rejected_proposal_is_remembered_and_never_proposed_again(
    worker: WorkerContext,
) -> None:
    _, _, key = await previewed(worker)
    await review(worker, 1001, body=f"/rastro reject {key[:6]}")

    first, _ = await merge(worker)

    assert (first.created, first.remembered) == (0, 1)
    [memory] = await all_rules(worker)
    assert (memory.statement, memory.status) == (FRIDAY, "rejected")
    assert memory.embedding is not None and await worker.anchors(memory.id)
    [entry] = await reviews(worker)
    assert entry.verdict == "rejected"
    # a later commit that makes the model propose the same rule again is suppressed
    worker.stage_commit(SHA4, {AGENDA: AFTER_SATURDAY}, {AGENDA: AFTER})
    await worker.add_commit_row(SHA4)
    await worker.add_session(SHA4, [user_excerpt("Implants cannot be booked on Friday afternoon.")])
    worker.llm.queue(RuleExtraction(rules=[friday()]))

    again = await extract_rules(worker.deps, worker.repository_id, SHA4)

    assert (again.created, again.suppressed) == (0, 1)


async def update_scenario(worker: WorkerContext, state_command: str | None) -> uuid.UUID:
    """An existing rule that the PR's first commit changes; optionally reviewed with a command."""
    worker.github.set_tree(worker.full_name, "1" * 40, {AGENDA: BEFORE}, head_of="main")
    await index_repository(worker.deps, worker.repository_id)
    existing = await worker.add_rule(
        FRIDAY,
        {(AGENDA, "AgendaService.validate"): "primary"},
        rationale="The doctor needs 48 hours of follow-up",
    )
    await stage_pull_request(worker)
    worker.stage_pull_request(NUMBER, {AGENDA: AFTER}, {AGENDA: BEFORE})
    worker.llm.queue(verdict("none"))  # the impact analysis of the rule the PR touches comes first
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
    await analyze(worker)
    worker.github.set_permission(worker.full_name, ANA, "write")
    if state_command:
        [row] = [r for r in await _proposals(worker) if r.commit_sha == SHA2]
        await review(worker, 1001, body=f"/rastro {state_command} {row.proposal_key[:6]}")
    return existing


async def _proposals(worker: WorkerContext) -> list[RuleProposal]:
    async with worker.deps.session_factory() as session:
        return list(
            await session.scalars(
                select(RuleProposal).where(RuleProposal.repository_id == worker.repository_id)
            )
        )


async def test_a_confirmed_update_changes_the_existing_rule_and_validates_the_new_revision(
    worker: WorkerContext,
) -> None:
    existing = await update_scenario(worker, "ok")

    await merge(worker)

    [rule_row] = await all_rules(worker)
    assert rule_row.id == existing and rule_row.statement == SATURDAY
    assert rule_row.revision == 2 and rule_row.validated_revision == 2  # not the old revision
    assert rule_row.rationale == "The doctor needs 48 hours of follow-up"  # kept


async def test_a_rejected_change_leaves_the_existing_rule_as_it_was(worker: WorkerContext) -> None:
    existing = await update_scenario(worker, "reject")

    first, _ = await merge(worker)

    [rule_row] = await all_rules(worker)
    assert (rule_row.id, rule_row.statement, rule_row.status) == (existing, FRIDAY, "active")
    assert (rule_row.revision, first.remembered, first.updated) == (1, 0, 0)
    assert (await proposal_states(worker))[SHA2] == "applied"  # consumed, nothing written


async def test_a_proposal_a_later_commit_replaced_is_not_applied(worker: WorkerContext) -> None:
    await stage_pull_request(worker)
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[rule(statement=SATURDAY)]))
    await analyze_pr_for_merge(worker)
    calls = len(worker.llm.calls)

    await merge(worker)

    [only] = await all_rules(worker)
    assert only.statement == SATURDAY  # last commit wins, like before
    assert len(worker.llm.calls) == calls


async def analyze_pr_for_merge(worker: WorkerContext) -> None:
    worker.stage_pull_request(NUMBER, {AGENDA: AFTER}, {AGENDA: BEFORE})
    await analyze(worker)


async def test_a_commit_that_was_never_previewed_takes_the_normal_path(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)  # no pull request analysis ran: no preview
    worker.llm.queue(RuleExtraction(rules=[friday()]))
    worker.llm.queue(RuleExtraction(rules=[]))

    first, _ = await merge(worker)

    assert first.from_preview is False and first.created == 1
    assert len(worker.llm.calls) == 2


async def test_a_session_that_arrived_after_the_preview_makes_it_stale(
    worker: WorkerContext,
) -> None:
    await previewed(worker)
    await worker.add_session(SHA2, [user_excerpt("Also: the same goes for Saturday.")])
    worker.llm.queue(RuleExtraction(rules=[rule(statement=SATURDAY)]))
    worker.llm.queue(RuleExtraction(rules=[]))
    calls = len(worker.llm.calls)

    first, _ = await merge(worker)

    assert first.from_preview is False and len(worker.llm.calls) > calls
    assert [r.statement for r in await all_rules(worker)] == [SATURDAY]


async def test_running_it_twice_does_not_duplicate_rules(worker: WorkerContext) -> None:
    await previewed(worker)
    await merge(worker)

    await merge(worker)
    forced = await merge(worker, force=True)  # even a forced re-run does not ask the model

    assert len(await all_rules(worker)) == 1
    assert [s.from_preview for s in forced] == [True, True] and forced[0].created == 0


async def test_no_credit_at_merge_leaves_the_reviews_waiting(worker: WorkerContext) -> None:
    _, _, key = await previewed(worker)
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")
    real_embed = worker.llm.embed

    async def broke(texts, *, metadata=None):  # type: ignore[no-untyped-def]
        raise LLMQuotaError("credit_balance_exhausted")

    worker.llm.embed = broke  # type: ignore[method-assign]
    first, _ = await merge(worker)
    assert first.status == "skipped_quota" and await all_rules(worker) == []
    assert (await proposal_states(worker))[SHA2] == "confirmed"  # still waiting, review intact

    worker.llm.embed = real_embed  # type: ignore[method-assign]
    await merge(worker, force=True)  # what `requeue` does once credit is back

    [created] = await all_rules(worker)
    assert (created.statement, created.validated_by) == (FRIDAY, ANA)


async def test_a_reviewer_who_is_the_commit_author_is_recorded_as_such(
    worker: WorkerContext,
) -> None:
    _, _, key = await previewed(worker)
    async with worker.deps.session_factory() as session, session.begin():
        await session.execute(
            update(Commit)
            .where(Commit.repository_id == worker.repository_id, Commit.sha == SHA2)
            .values(author="Ana")
        )
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")

    await merge(worker)

    [entry] = await reviews(worker)
    assert entry.reviewer_is_session_author is True


async def test_an_unknown_author_stays_unknown(worker: WorkerContext) -> None:
    _, _, key = await previewed(worker)
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")

    await merge(worker)

    [entry] = await reviews(worker)
    assert entry.reviewer_is_session_author is None  # never a guess


async def record_pr_author(worker: WorkerContext, login: str | None) -> None:
    async with worker.deps.session_factory() as session, session.begin():
        await session.execute(
            update(RuleProposal)
            .where(RuleProposal.repository_id == worker.repository_id)
            .values(pr_author=login)
        )


async def test_a_reviewer_other_than_the_pr_author_is_recorded_as_such(
    worker: WorkerContext,
) -> None:
    _, _, key = await previewed(worker)
    await record_pr_author(worker, "dev-bo")
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")

    await merge(worker)

    [entry] = await reviews(worker)
    assert entry.reviewer_is_pr_author is False


async def test_the_pr_author_confirming_their_own_proposal_is_recorded_as_such(
    worker: WorkerContext,
) -> None:
    _, _, key = await previewed(worker)
    await record_pr_author(worker, ANA.upper())  # logins are compared without regard to case
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")

    await merge(worker)

    [entry] = await reviews(worker)
    assert entry.reviewer_is_pr_author is True


async def test_without_a_recorded_pr_author_the_flag_stays_unknown(worker: WorkerContext) -> None:
    _, _, key = await previewed(worker)
    await record_pr_author(worker, None)
    await review(worker, 1001, body=f"/rastro ok {key[:6]}")

    await merge(worker)

    [entry] = await reviews(worker)
    assert entry.reviewer_is_pr_author is None  # never a guess


async def test_the_pr_author_only_fills_proposals_that_have_none(worker: WorkerContext) -> None:
    await previewed(worker)

    await set_pr_author(worker.deps, worker.repository_id, NUMBER, "dev-ana")
    await set_pr_author(worker.deps, worker.repository_id, NUMBER, "someone-else")
    await set_pr_author(worker.deps, worker.repository_id, NUMBER, None)

    proposals = await _proposals(worker)
    assert proposals and {p.pr_author for p in proposals} == {"dev-ana"}
