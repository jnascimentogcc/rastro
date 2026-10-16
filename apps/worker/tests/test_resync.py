import uuid

import pytest
from procrastinate import testing
from worker_support import WorkerContext

from rastro_github import ChangedFile
from rastro_llm import hashing_embedding
from rastro_worker import tasks
from rastro_worker.app import app
from rastro_worker.indexing import analyze_commit, index_repository
from rastro_worker.resync import resync_anchors

SHA1, SHA2, SHA3 = "1" * 40, "2" * 40, "3" * 40
AGENDA = "src/agenda.ts"

RULE_FN = """export function validateBooking(day: number, hour: number, kind: string): boolean {
  if (kind === 'implant' && day === 5 && hour >= 12) {
    return false;
  }
  return true;
}
"""
RENAMED = RULE_FN.replace("validateBooking", "canBook")
OTHER = "export function keepMe(a: number) {\n  const b = a + 1;\n  return b;\n}\n"
REWRITTEN = """export function isAllowed(request: { treatment: string; weekday: number; time: number }) {
  const blocked = request.treatment === 'implant' && request.weekday === 5;
  return !(blocked && request.time >= 12);
}
"""

PRIMARY = {(AGENDA, "validateBooking"): "primary"}


async def index(worker: WorkerContext, files: dict[str, str]) -> None:
    worker.github.set_tree(worker.full_name, SHA1, files, head_of="main")
    await index_repository(worker.deps, worker.repository_id)


async def commit(
    worker: WorkerContext, sha: str, files: dict[str, str], changes: dict[str, str | None]
) -> None:
    """Apply a commit on the default branch: static analysis, then resync."""
    worker.stage_commit(sha, files, changes)
    await analyze_commit(worker.deps, worker.repository_id, sha, True)


async def test_renamed_function_keeps_its_rule(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN})
    rule_id = await worker.add_rule("No implants on Friday afternoon", PRIMARY)

    await commit(worker, SHA2, {AGENDA: RENAMED}, {AGENDA: RULE_FN})
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and (result.by_hash, result.orphaned) == (1, 0)
    assert await worker.anchors(rule_id) == {(AGENDA, "canBook"): "primary"}
    assert await worker.rule_status(rule_id) == "active"
    assert (AGENDA, "validateBooking") in await worker.symbols(status="removed")


async def test_function_moved_to_another_file_is_followed_by_name(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN + OTHER})
    rule_id = await worker.add_rule("No implants on Friday afternoon", PRIMARY)

    await commit(
        worker,
        SHA2,
        {AGENDA: OTHER, "src/booking.ts": RULE_FN},
        {AGENDA: RULE_FN + OTHER, "src/booking.ts": None},
    )
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and result.by_name == 1
    assert await worker.anchors(rule_id) == {("src/booking.ts", "validateBooking"): "primary"}


async def test_deleted_code_orphans_the_rule(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN + OTHER})
    rule_id = await worker.add_rule("No implants on Friday afternoon", PRIMARY)

    await commit(worker, SHA2, {AGENDA: OTHER}, {AGENDA: RULE_FN + OTHER})
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and (result.reanchored, result.orphaned) == (0, 1)
    assert await worker.rule_status(rule_id) == "orphaned"
    assert set(await worker.anchors(rule_id)) == {(AGENDA, "validateBooking")}  # history is kept


async def test_a_rule_with_a_surviving_anchor_stays_active(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN + OTHER})
    rule_id = await worker.add_rule(
        "Shared rule", {(AGENDA, "validateBooking"): "primary", (AGENDA, "keepMe"): "primary"}
    )

    await commit(worker, SHA2, {AGENDA: OTHER}, {AGENDA: RULE_FN + OTHER})
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and result.orphaned == 0
    assert await worker.rule_status(rule_id) == "active"


async def test_rewritten_code_is_found_by_embedding_similarity(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN})
    rule_id = await worker.add_rule("No implants on Friday afternoon", PRIMARY)
    vector = hashing_embedding("implant friday afternoon booking validation", 1024)
    await worker.set_embedding(AGENDA, "validateBooking", vector)

    await commit(worker, SHA2, {AGENDA: REWRITTEN}, {AGENDA: RULE_FN})
    await worker.set_embedding(AGENDA, "isAllowed", vector)  # the new code embeds the same way
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and (result.by_embedding, result.orphaned) == (1, 0)
    assert await worker.anchors(rule_id) == {(AGENDA, "isAllowed"): "primary"}


async def test_dissimilar_rewrite_is_not_followed(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN})
    rule_id = await worker.add_rule("No implants on Friday afternoon", PRIMARY)
    await worker.set_embedding(
        AGENDA, "validateBooking", hashing_embedding("alpha beta gamma", 1024)
    )

    await commit(worker, SHA2, {AGENDA: REWRITTEN}, {AGENDA: RULE_FN})
    await worker.set_embedding(AGENDA, "isAllowed", hashing_embedding("zeta eta theta iota", 1024))
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and result.reanchored == 0
    assert await worker.rule_status(rule_id) == "orphaned"


async def test_code_that_comes_back_later_reactivates_the_rule(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN + OTHER})
    rule_id = await worker.add_rule("No implants on Friday afternoon", PRIMARY)
    await commit(worker, SHA2, {AGENDA: OTHER}, {AGENDA: RULE_FN + OTHER})
    await resync_anchors(worker.deps, worker.repository_id)
    assert await worker.rule_status(rule_id) == "orphaned"

    await commit(
        worker,
        SHA3,
        {AGENDA: OTHER, "src/rules.ts": RENAMED},
        {"src/rules.ts": None},
    )
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and (result.by_hash, result.reactivated) == (1, 1)
    assert await worker.rule_status(rule_id) == "active"
    assert await worker.anchors(rule_id) == {("src/rules.ts", "canBook"): "primary"}


async def test_successor_already_linked_as_duplicate_is_promoted(worker: WorkerContext) -> None:
    copy = RULE_FN.replace("validateBooking", "validateCopy")
    await index(worker, {AGENDA: RULE_FN, "src/copy.ts": copy})
    rule_id = await worker.add_rule(
        "No implants on Friday afternoon",
        {(AGENDA, "validateBooking"): "primary", ("src/copy.ts", "validateCopy"): "duplicate"},
    )

    worker.github.set_tree(worker.full_name, SHA2, {"src/copy.ts": copy})
    worker.github.add_commit(worker.full_name, SHA2, [ChangedFile(AGENDA, "removed")])
    await analyze_commit(worker.deps, worker.repository_id, SHA2, True)
    result = await resync_anchors(worker.deps, worker.repository_id)

    assert result is not None and result.by_hash == 1
    assert await worker.anchors(rule_id) == {("src/copy.ts", "validateCopy"): "primary"}


async def test_embedding_is_carried_over_to_the_successor(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN})
    await worker.add_rule("No implants on Friday afternoon", PRIMARY)
    vector = hashing_embedding("implant friday", 1024)
    await worker.set_embedding(AGENDA, "validateBooking", vector)

    await commit(worker, SHA2, {AGENDA: RENAMED}, {AGENDA: RULE_FN})
    await resync_anchors(worker.deps, worker.repository_id)

    carried = await worker.embedding_of(AGENDA, "canBook")
    assert carried is not None and list(carried) == pytest.approx(list(vector), abs=1e-6)


async def test_resync_is_idempotent(worker: WorkerContext) -> None:
    await index(worker, {AGENDA: RULE_FN})
    await worker.add_rule("No implants on Friday afternoon", PRIMARY)
    await commit(worker, SHA2, {AGENDA: RENAMED}, {AGENDA: RULE_FN})
    await resync_anchors(worker.deps, worker.repository_id)

    again = await resync_anchors(worker.deps, worker.repository_id)

    assert again is not None
    assert (again.reanchored, again.orphaned, again.reactivated) == (0, 0, 0)


async def test_unknown_repository_is_ignored(worker: WorkerContext) -> None:
    assert await resync_anchors(worker.deps, uuid.uuid4()) is None


async def test_default_branch_analysis_and_indexing_queue_a_resync(worker: WorkerContext) -> None:
    worker.github.set_tree(worker.full_name, SHA1, {AGENDA: RULE_FN}, head_of="main")
    worker.stage_commit(SHA2, {AGENDA: RENAMED}, {AGENDA: RULE_FN})
    connector = testing.InMemoryConnector()
    repo = str(worker.repository_id)

    with app.replace_connector(connector) as test_app:
        async with test_app.open_async():
            await tasks.index_repository(repo)
            await tasks.analyze_commit(repo, SHA2, True)
            await tasks.analyze_commit(repo, SHA2, False)

    jobs = [j for j in connector.jobs.values() if j["task_name"] == "resync_anchors"]
    assert len(jobs) == 2  # after the index and after the default-branch commit only
    assert all(j["lock"] == repo for j in jobs)
