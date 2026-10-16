import uuid

from procrastinate import testing
from sqlalchemy import select
from worker_support import WorkerContext, user_excerpt

from rastro_db.models import LlmCall, RuleOrigin
from rastro_db.models import Symbol as SymbolRow
from rastro_llm import ExtractedRule, LLMError, RuleExtraction, hashing_embedding
from rastro_worker import tasks
from rastro_worker.app import app
from rastro_worker.rule_extraction import extract_rules

SHA1, SHA2, SHA3 = "1" * 40, "2" * 40, "3" * 40
AGENDA = "src/agenda.ts"
KEY = f"{AGENDA}::AgendaService.validate"

BEFORE = """export class AgendaService {
  validate(day: number, hour: number, kind: string): boolean {
    return true;
  }
}
"""
AFTER = """export class AgendaService {
  validate(day: number, hour: number, kind: string): boolean {
    if (kind === 'implant' && day === 5 && hour >= 12) {
      return false;
    }
    return true;
  }
}
"""
AFTER_SATURDAY = AFTER.replace("day === 5", "(day === 5 || day === 6)")

REQUEST = "Implants cannot be booked on Friday afternoon. Dr. Mendes needs 48h of follow-up."


def rule(**overrides: object) -> ExtractedRule:
    values: dict[str, object] = {
        "action": "create",
        "existing_rule_id": None,
        "statement": "Implants cannot be booked on Friday afternoon",
        "rationale": "The doctor needs 48 hours of follow-up",
        "origin_quote": "Dr. Mendes needs 48h of follow-up",
        "symbols": [KEY],
        "confidence": 0.9,
    }
    return ExtractedRule(**(values | overrides))


async def stage_friday_commit(worker: WorkerContext, request: str = REQUEST) -> None:
    worker.stage_commit(SHA2, {AGENDA: AFTER}, {AGENDA: BEFORE})
    await worker.add_commit_row(SHA2, "Block implants on Friday")
    await worker.add_session(SHA2, [user_excerpt(request)])


async def test_creates_a_rule_with_verified_rationale_and_anchors(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert (summary.status, summary.created, summary.discarded) == ("analyzed", 1, 0)
    [stored] = await worker.rules()
    assert stored.statement == "Implants cannot be booked on Friday afternoon"
    assert stored.rationale == "The doctor needs 48 hours of follow-up"
    assert stored.embedding is not None and stored.status == "active"
    assert await worker.anchors(stored.id) == {(AGENDA, "AgendaService.validate"): "primary"}
    async with worker.deps.session_factory() as session:
        origin = await session.scalar(select(RuleOrigin).where(RuleOrigin.rule_id == stored.id))
        symbol = await session.scalar(
            select(SymbolRow).where(
                SymbolRow.qualified_name == "AgendaService.validate",
                SymbolRow.repository_id == worker.repository_id,
            )
        )
    assert origin is not None and (origin.commit_sha, origin.quote) == (
        SHA2,
        "Dr. Mendes needs 48h of follow-up",
    )
    assert symbol is not None and symbol.embedding is not None
    commit = await worker.commit(SHA2)
    assert commit is not None and commit.analysis_status == "analyzed" and commit.analyzed_at


async def test_prompt_carries_request_diff_and_changed_symbol_only(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[]))

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    prompt = worker.llm.calls[0].prompt
    assert REQUEST in prompt and "Block implants on Friday" in prompt
    assert "+    if (kind === 'implant'" in prompt
    assert f'<symbol key="{KEY}" kind="method">' in prompt
    assert (
        'key="src/agenda.ts::AgendaService" kind="class"' not in prompt
    )  # only the method changed


async def test_an_invented_rationale_is_dropped(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(
                    rationale="The clinic closes for deep cleaning on weekends",
                    origin_quote="the clinic closes on weekends",
                )
            ]
        )
    )

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    [stored] = await worker.rules()
    assert stored.rationale is None  # never invent the why
    async with worker.deps.session_factory() as session:
        origin = await session.scalar(select(RuleOrigin).where(RuleOrigin.rule_id == stored.id))
    assert origin is not None and origin.quote is None  # and no made-up citation


async def test_a_paraphrased_rationale_is_kept_but_an_unverifiable_quote_is_not(
    worker: WorkerContext,
) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(
                    rationale="Dr. Mendes needs two days of follow-up after an implant",
                    origin_quote="paraphrased, not verbatim",
                )
            ]
        )
    )

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    [stored] = await worker.rules()
    assert stored.rationale == "Dr. Mendes needs two days of follow-up after an implant"
    async with worker.deps.session_factory() as session:
        origin = await session.scalar(select(RuleOrigin).where(RuleOrigin.rule_id == stored.id))
    assert origin is not None and origin.quote is None


async def test_quote_check_ignores_case_and_whitespace(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(
        RuleExtraction(rules=[rule(origin_quote="DR.  MENDES needs\n48H of follow-up")])
    )

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    [stored] = await worker.rules()
    assert stored.rationale is not None


async def test_rule_without_stated_reason_keeps_rationale_null(worker: WorkerContext) -> None:
    await stage_friday_commit(worker, "Give a discount to customers over 65")
    worker.llm.queue(
        RuleExtraction(
            rules=[rule(rationale=None, origin_quote="Give a discount to customers over 65")]
        )
    )

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    [stored] = await worker.rules()
    assert stored.rationale is None


async def test_invalid_anchors_low_confidence_and_none_are_discarded(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(symbols=["src/made_up.ts::ghost"]),
                rule(confidence=0.49),
                rule(action="none", symbols=[]),
                rule(statement="   "),
                rule(symbols=[]),
            ]
        )
    )

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert (summary.created, summary.discarded) == (0, 5)
    assert await worker.rules() == []


async def test_technical_only_answer_creates_no_rules(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[]))

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "analyzed" and await worker.rules() == []


async def test_a_later_commit_updates_the_rule_instead_of_creating_another(
    worker: WorkerContext,
) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))
    await extract_rules(worker.deps, worker.repository_id, SHA2)
    [first] = await worker.rules()

    worker.stage_commit(SHA3, {AGENDA: AFTER_SATURDAY}, {AGENDA: AFTER})
    await worker.add_commit_row(SHA3)
    await worker.add_session(SHA3, [user_excerpt("Also block Saturday for implants")])
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(
                    action="update",
                    existing_rule_id=str(first.id),
                    statement="Implants cannot be booked on Friday afternoon or on Saturday",
                    rationale=None,
                    origin_quote="Also block Saturday for implants",
                )
            ]
        )
    )

    summary = await extract_rules(worker.deps, worker.repository_id, SHA3)

    assert (summary.created, summary.updated) == (0, 1)
    assert f'<rule id="{first.id}"' in worker.llm.calls[1].prompt  # the model saw the old rule
    [only] = await worker.rules()
    assert only.id == first.id
    assert only.statement.endswith("or on Saturday")
    assert only.rationale == "The doctor needs 48 hours of follow-up"  # not erased
    async with worker.deps.session_factory() as session:
        origins = (
            await session.scalars(
                select(RuleOrigin.commit_sha).where(RuleOrigin.rule_id == first.id)
            )
        ).all()
    assert sorted(origins) == [SHA2, SHA3]


async def test_an_update_that_changes_nothing_is_ignored(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))
    await extract_rules(worker.deps, worker.repository_id, SHA2)
    [first] = await worker.rules()

    refactored = AFTER.replace("kind", "treatment")
    worker.stage_commit(SHA3, {AGENDA: refactored}, {AGENDA: AFTER})
    await worker.add_commit_row(SHA3)
    await worker.add_session(SHA3, [user_excerpt("Rename the parameter, no behaviour change")])
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(
                    action="update",
                    existing_rule_id=str(first.id),
                    rationale=None,
                    origin_quote=None,
                )
            ]
        )
    )

    summary = await extract_rules(worker.deps, worker.repository_id, SHA3)

    assert (summary.created, summary.updated, summary.unchanged) == (0, 0, 1)
    assert summary.rule_ids == []
    async with worker.deps.session_factory() as session:
        origins = (
            await session.scalars(
                select(RuleOrigin.commit_sha).where(RuleOrigin.rule_id == first.id)
            )
        ).all()
    assert list(origins) == [SHA2]  # the refactor is not recorded as an origin


async def test_update_of_an_unknown_rule_becomes_a_new_rule(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(
        RuleExtraction(rules=[rule(action="update", existing_rule_id=str(uuid.uuid4()))])
    )

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert (summary.created, summary.updated) == (1, 0)


async def test_exact_duplicate_in_an_untouched_file_is_linked(worker: WorkerContext) -> None:
    calendar = AFTER.replace("AgendaService", "CalendarRules").replace("validate", "allowed")
    worker.github.set_tree(
        worker.full_name, SHA1, {AGENDA: BEFORE, "src/calendar.ts": calendar}, head_of="main"
    )
    from rastro_worker.indexing import index_repository

    await index_repository(worker.deps, worker.repository_id)
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.duplicates_linked == 1
    [stored] = await worker.rules()
    assert await worker.anchors(stored.id) == {
        (AGENDA, "AgendaService.validate"): "primary",
        ("src/calendar.ts", "CalendarRules.allowed"): "duplicate",
    }


async def test_similar_code_with_an_embedding_is_linked_even_when_the_hash_differs(
    worker: WorkerContext,
) -> None:
    near = AFTER.replace("AgendaService", "Other").replace("hour >= 12", "hour >= 13")
    worker.github.set_tree(
        worker.full_name, SHA1, {AGENDA: BEFORE, "src/other.ts": near}, head_of="main"
    )
    from rastro_worker.indexing import index_repository

    await index_repository(worker.deps, worker.repository_id)
    # Give the other symbol the embedding the primary will get (same words -> distance ~0).
    method_text = "\n".join(AFTER.splitlines()[1:7])
    async with worker.deps.session_factory() as session, session.begin():
        other = await session.scalar(
            select(SymbolRow).where(
                SymbolRow.file_path == "src/other.ts", SymbolRow.qualified_name == "Other.validate"
            )
        )
        assert other is not None
        other.embedding = hashing_embedding(method_text, 1024)
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.duplicates_linked == 1
    [stored] = await worker.rules()
    assert (await worker.anchors(stored.id))[("src/other.ts", "Other.validate")] == "duplicate"


async def test_one_failed_answer_is_retried(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(LLMError("invalid output"))
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "analyzed" and summary.created == 1
    assert len(worker.llm.calls) == 2


async def test_two_failures_mark_the_commit_without_raising(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(LLMError("refused"))
    worker.llm.queue(LLMError("refused again"))

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "analysis_failed"
    commit = await worker.commit(SHA2)
    assert (
        commit is not None
        and commit.analysis_status == "analysis_failed"
        and commit.analyzed_at is None
    )
    assert await worker.rules() == []


async def test_commit_without_a_session_is_skipped_without_calling_the_model(
    worker: WorkerContext,
) -> None:
    worker.stage_commit(SHA2, {AGENDA: AFTER}, {AGENDA: BEFORE})

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "skipped_no_session" and worker.llm.calls == []
    commit = await worker.commit(SHA2)
    assert commit is not None and commit.analysis_status == "skipped_no_session"


async def test_commit_that_touches_no_source_symbols_needs_no_model_call(
    worker: WorkerContext,
) -> None:
    worker.stage_commit(SHA2, {"README.md": "# new"}, {"README.md": "# old"})
    await worker.add_session(SHA2, [user_excerpt("update the readme")])

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "analyzed" and worker.llm.calls == []


async def test_without_an_llm_nothing_happens(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.deps.llm = None

    summary = await extract_rules(worker.deps, worker.repository_id, SHA2)

    assert summary.status == "skipped_no_llm"


async def test_analyzed_commit_is_not_reprocessed_until_a_new_session_or_force(
    worker: WorkerContext,
) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[]))
    await extract_rules(worker.deps, worker.repository_id, SHA2)
    assert len(worker.llm.calls) == 1

    await extract_rules(worker.deps, worker.repository_id, SHA2)
    assert len(worker.llm.calls) == 1  # unchanged -> skipped

    await worker.add_session(SHA2, [user_excerpt("a second session arrived late")])
    worker.llm.queue(RuleExtraction(rules=[]))
    await extract_rules(worker.deps, worker.repository_id, SHA2)
    assert len(worker.llm.calls) == 2

    worker.llm.queue(RuleExtraction(rules=[]))
    await extract_rules(worker.deps, worker.repository_id, SHA2, force=True)
    assert len(worker.llm.calls) == 3


async def test_model_calls_are_recorded_for_cost_control(worker: WorkerContext) -> None:
    await stage_friday_commit(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    async with worker.deps.session_factory() as session:
        calls = (
            await session.scalars(
                select(LlmCall).where(LlmCall.repository_id == worker.repository_id)
            )
        ).all()
    assert {c.purpose for c in calls} == {"extraction"}  # fake embeddings are not metered
    assert calls[0].model == "fake" and calls[0].input_tokens > 0


async def test_analyze_commit_task_queues_extraction_only_when_sessions_exist(
    worker: WorkerContext,
) -> None:
    await stage_friday_commit(worker)
    connector = testing.InMemoryConnector()
    with app.replace_connector(connector) as test_app:
        async with test_app.open_async():
            await tasks.analyze_commit(str(worker.repository_id), SHA2, False)
            await tasks.analyze_commit(str(worker.repository_id), SHA3, False)

    jobs = [j for j in connector.jobs.values() if j["task_name"] == "rule_extraction"]
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"repository_id": str(worker.repository_id), "sha": SHA2}
    assert jobs[0]["lock"] == str(worker.repository_id)


async def test_only_user_intent_text_can_justify_a_rationale(worker: WorkerContext) -> None:
    """An assistant note must not be accepted as the origin of a reason."""
    worker.stage_commit(SHA2, {AGENDA: AFTER}, {AGENDA: BEFORE})
    note: dict[str, object] = {
        "role": "assistant",
        "kind": "assistant_note",
        "text": "Dr. Mendes needs 48h of follow-up",
        "timestamp": "t",
        "files": [],
    }
    await worker.add_session(SHA2, [user_excerpt("block implants on Friday"), note])
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    await extract_rules(worker.deps, worker.repository_id, SHA2)

    [stored] = await worker.rules()
    assert stored.rationale is None
