import uuid
from datetime import UTC, datetime

from procrastinate import testing
from worker_support import WorkerContext

from rastro_llm import ImpactAssessment, LLMError
from rastro_worker import tasks
from rastro_worker.app import app
from rastro_worker.comment import MARKER
from rastro_worker.impact import analyze_pull_request
from rastro_worker.indexing import index_repository

SHA1, HEAD1, HEAD2 = "1" * 40, "a" * 40, "b" * 40
AGENDA, CALENDAR = "src/agenda.ts", "src/calendar.ts"

AGENDA_V1 = """export function validateBooking(day: number, hour: number, kind: string): boolean {
  if (kind === 'implant' && day === 5 && hour >= 12) {
    return false;
  }
  return true;
}
"""
CALENDAR_V1 = AGENDA_V1.replace("validateBooking", "isAllowed")
AGENDA_SATURDAY = AGENDA_V1.replace(
    "day === 5 && hour >= 12", "(day === 5 && hour >= 12) || day === 6"
)
PRICING = "export function price(a: number) {\n  const b = a * 2;\n  return b;\n}\n"

PRIMARY_AND_COPY = {(AGENDA, "validateBooking"): "primary", (CALENDAR, "isAllowed"): "duplicate"}
PR = 5


def verdict(
    risk: str = "changes", confidence: float = 0.9, text: str = "It changes the rule."
) -> ImpactAssessment:
    changed = risk != "none"
    return ImpactAssessment(
        behavior_changed=changed,
        counterexample="A Saturday 09:00 implant is now accepted." if changed else None,
        risk=risk,
        confidence=confidence,
        explanation=text,
    )


async def seeded(worker: WorkerContext, files: dict[str, str] | None = None) -> uuid.UUID:
    """Index the default branch and anchor one rule to validateBooking (+ its copy)."""
    worker.github.set_tree(
        worker.full_name, SHA1, files or {AGENDA: AGENDA_V1, CALENDAR: CALENDAR_V1}, head_of="main"
    )
    await index_repository(worker.deps, worker.repository_id)
    rule_id = await worker.add_rule(
        "Implants cannot be booked on Friday afternoon",
        PRIMARY_AND_COPY if files is None else {(AGENDA, "validateBooking"): "primary"},
        rationale="Dr. Mendes needs 48h of follow-up",
    )
    await worker.add_origin(rule_id, "c0ffee1234567", datetime(2026, 3, 12, tzinfo=UTC))
    return rule_id


async def run(worker: WorkerContext, head: str = HEAD1):  # type: ignore[no-untyped-def]
    return await analyze_pull_request(worker.deps, worker.repository_id, PR, head)


async def test_a_pull_request_that_changes_a_rule_gets_one_comment(worker: WorkerContext) -> None:
    rule_id = await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("changes", 0.9, "This change enables Saturday bookings."))

    result = await run(worker)

    assert (result.candidates, result.assessed, result.reported, result.commented) == (
        1,
        1,
        1,
        True,
    )
    [body] = worker.github.comments_on(PR)
    assert body.startswith(MARKER)
    assert "**Implants cannot be booked on Friday afternoon**" in body
    assert "Why: Dr. Mendes needs 48h of follow-up" in body
    assert "Risk: This change enables Saturday bookings." in body
    assert "the same logic exists in `src/calendar.ts` and was not changed" in body
    assert "Origin: request of 03/12, commit c0ffee1." in body
    analysis = await worker.pr_analysis(PR)
    assert (
        analysis is not None
        and analysis.head_sha == HEAD1
        and analysis.comment_id == result.comment_id
    )
    assert (
        analysis.findings[0]["rule_id"] == str(rule_id) and analysis.findings[0]["reported"] is True
    )
    prompt = worker.llm.calls[0].prompt
    assert "(day === 5 && hour >= 12) || day === 6" in prompt  # the head code of the touched symbol
    assert "src/calendar.ts::isAllowed" in prompt  # the unchanged copy is named
    assert worker.llm.calls[0].purpose == "impact"


async def test_a_new_push_edits_the_same_comment_instead_of_adding_another(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("changes", 0.9, "First explanation."))
    first = await run(worker, HEAD1)
    worker.llm.queue(verdict("breaks", 0.95, "Second explanation."))

    second = await run(worker, HEAD2)

    assert second.comment_id == first.comment_id
    [body] = worker.github.comments_on(PR)
    assert "Second explanation." in body and "First explanation." not in body
    analysis = await worker.pr_analysis(PR)
    assert analysis is not None and analysis.head_sha == HEAD2  # still a single row for the PR


async def test_findings_below_the_threshold_or_without_risk_stay_silent(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})

    worker.llm.queue(verdict("breaks", 0.69))
    low = await run(worker)
    worker.llm.queue(verdict("none", 0.99))
    none = await run(worker)

    assert (low.assessed, low.reported, low.commented) == (1, 0, False)
    assert (none.assessed, none.reported, none.commented) == (1, 0, False)
    assert worker.github.comments == {}
    analysis = await worker.pr_analysis(PR)
    assert analysis is not None and analysis.findings[0]["reported"] is False


async def test_the_threshold_is_configurable(worker: WorkerContext) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.deps.impact.impact_threshold = 0.5
    worker.llm.queue(verdict("breaks", 0.6))

    assert (await run(worker)).reported == 1


async def test_a_resolved_warning_turns_the_comment_into_an_all_clear(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("changes", 0.9))
    flagged = await run(worker, HEAD1)
    worker.llm.queue(verdict("none", 0.95))

    cleared = await run(worker, HEAD2)

    assert cleared.comment_id == flagged.comment_id and cleared.commented
    [body] = worker.github.comments_on(PR)
    assert "no rules affected" in body and "`bbbbbbb`" in body


async def test_a_deleted_comment_is_replaced(worker: WorkerContext) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("changes", 0.9))
    first = await run(worker, HEAD1)
    assert first.comment_id is not None
    worker.github.delete_comment(first.comment_id)
    worker.llm.queue(verdict("changes", 0.9))

    second = await run(worker, HEAD2)

    assert second.comment_id != first.comment_id
    assert len(worker.github.comments_on(PR)) == 1


async def test_a_pull_request_that_touches_no_rule_costs_nothing(worker: WorkerContext) -> None:
    await seeded(worker, {AGENDA: AGENDA_V1, "src/pricing.ts": PRICING})
    worker.stage_pull_request(
        PR, {"src/pricing.ts": PRICING.replace("2", "3")}, {"src/pricing.ts": PRICING}
    )

    result = await run(worker)

    assert (result.candidates, result.assessed, result.commented) == (0, 0, False)
    assert worker.llm.calls == [] and worker.github.comments == {}


async def test_a_rename_is_not_a_candidate_because_the_rule_will_follow_it(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(
        PR, {AGENDA: AGENDA_V1.replace("validateBooking", "canBook")}, {AGENDA: AGENDA_V1}
    )

    result = await run(worker)

    assert result.candidates == 0 and worker.llm.calls == []


async def test_a_deleted_function_is_a_candidate(worker: WorkerContext) -> None:
    await seeded(worker, {AGENDA: AGENDA_V1 + PRICING})
    worker.stage_pull_request(PR, {AGENDA: PRICING}, {AGENDA: AGENDA_V1 + PRICING})
    worker.llm.queue(verdict("breaks", 0.9, "The check was removed."))

    result = await run(worker)

    assert result.reported == 1
    assert "(deleted or renamed by this change)" in worker.llm.calls[0].prompt


async def test_rules_near_the_change_in_the_call_graph_are_candidates(
    worker: WorkerContext,
) -> None:
    caller = "export function book(d: number) {\n  const ok = validateBooking(d, 9, 'x');\n  return ok;\n}\n"
    await seeded(worker, {AGENDA: AGENDA_V1, "src/booking.ts": caller})
    worker.stage_pull_request(
        PR, {"src/booking.ts": caller.replace("9", "20")}, {"src/booking.ts": caller}
    )
    worker.llm.queue(verdict("changes", 0.9, "Booking now passes another hour."))

    result = await run(worker)

    assert result.candidates == 1 and result.reported == 1
    prompt = worker.llm.calls[0].prompt
    assert '<rule relation="related">' in prompt
    assert "src/booking.ts::book" in prompt  # the changed neighbour is shown


async def test_an_invented_duplicate_warning_is_downgraded(worker: WorkerContext) -> None:
    await seeded(worker, {AGENDA: AGENDA_V1})  # the rule has no other copy
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("inconsistent_duplicate", 0.9, "A copy elsewhere disagrees."))

    result = await run(worker)

    assert result.reported == 1
    analysis = await worker.pr_analysis(PR)
    assert analysis is not None and analysis.findings[0]["risk"] == "changes"
    [body] = worker.github.comments_on(PR)
    assert "Warning: the same logic exists" not in body


async def test_a_model_failure_skips_that_rule_without_failing_the_job(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(LLMError("refused"))
    worker.llm.queue(LLMError("refused again"))

    result = await run(worker)

    assert (result.candidates, result.assessed, result.commented) == (1, 0, False)
    assert len(worker.llm.calls) == 2


async def test_the_number_of_model_calls_is_capped(worker: WorkerContext) -> None:
    await seeded(worker)
    await worker.add_rule("Second rule on the same code", {(AGENDA, "validateBooking"): "primary"})
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.deps.impact.impact_max_candidates = 1
    worker.llm.queue(verdict("none", 0.9))

    result = await run(worker)

    assert (result.candidates, len(worker.llm.calls)) == (1, 1)


async def test_comments_can_be_written_in_portuguese(worker: WorkerContext) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.deps.impact.impact_comment_language = "pt"
    worker.llm.queue(verdict("changes", 0.9))

    await run(worker)

    [body] = worker.github.comments_on(PR)
    assert "regras afetadas por este PR" in body and "Origem: pedido de 12/03" in body


async def test_without_an_llm_or_a_known_installation_nothing_happens(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.deps.llm = None
    assert (await run(worker)).status == "skipped_no_llm"

    worker.deps.llm = worker.llm
    unknown = await analyze_pull_request(worker.deps, uuid.uuid4(), PR, HEAD1)
    assert unknown.status == "skipped_unknown_repository"


async def test_the_task_runs_the_analysis(worker: WorkerContext) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("changes", 0.9))
    connector = testing.InMemoryConnector()

    with app.replace_connector(connector) as test_app:
        async with test_app.open_async():
            await tasks.analyze_pr(str(worker.repository_id), PR, HEAD1)

    assert len(worker.github.comments_on(PR)) == 1


async def test_a_risk_without_a_real_change_of_behaviour_is_not_reported(
    worker: WorkerContext,
) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    no_change = ImpactAssessment(
        behavior_changed=False,
        counterexample=None,
        risk="breaks",
        confidence=0.99,
        explanation="Looks risky.",
    )
    no_example = ImpactAssessment(
        behavior_changed=True,
        counterexample="  ",
        risk="breaks",
        confidence=0.99,
        explanation="Looks risky.",
    )

    worker.llm.queue(no_change)
    first = await run(worker)
    worker.llm.queue(no_example)
    second = await run(worker)

    assert (first.reported, second.reported) == (0, 0)  # evidence is mandatory
    assert worker.github.comments == {}
    analysis = await worker.pr_analysis(PR)
    assert analysis is not None and analysis.findings[0]["risk"] == "none"


async def test_the_counterexample_is_kept_with_the_stored_finding(worker: WorkerContext) -> None:
    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(verdict("changes", 0.9))

    await run(worker)

    analysis = await worker.pr_analysis(PR)
    assert analysis is not None
    assert analysis.findings[0]["counterexample"] == "A Saturday 09:00 implant is now accepted."


async def test_an_empty_llm_balance_skips_the_analysis_without_a_misleading_comment(
    worker: WorkerContext,
) -> None:
    from rastro_llm import LLMQuotaError

    await seeded(worker)
    worker.stage_pull_request(PR, {AGENDA: AGENDA_SATURDAY}, {AGENDA: AGENDA_V1})
    worker.llm.queue(LLMQuotaError("credit_balance_exhausted"))

    result = await run(worker)  # does not raise

    assert result.status == "skipped_quota" and not result.commented
    assert len(worker.llm.calls) == 1  # no retry
    assert worker.github.comments == {}  # silence must not read as "no rules affected"
    assert await worker.pr_analysis(PR) is None
