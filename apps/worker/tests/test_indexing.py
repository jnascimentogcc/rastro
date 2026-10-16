import pytest
from worker_support import WorkerContext

from rastro_db.models import AgentSession, Repository
from rastro_github import ChangedFile
from rastro_worker.indexing import analyze_commit, index_repository, is_indexable

SHA1, SHA2, SHA3 = "1" * 40, "2" * 40, "3" * 40

AGENDA = """
import { isFriday } from './calendar';
export function validateBooking(day: number, hour: number) {
  return !(isFriday(day) && hour >= 12);
}
export function unrelated() { return 1; }
"""
CALENDAR = "export function isFriday(day: number) { return day === 5; }\n"


@pytest.mark.parametrize(
    ("path", "size", "expected"),
    [
        ("src/a.ts", 100, True),
        ("src/Button.tsx", 100, True),
        ("lib/util.js", 100, True),
        ("README.md", 100, False),
        ("src/types.d.ts", 100, False),
        ("node_modules/pkg/index.js", 100, False),
        ("dist/app.js", 100, False),
        ("public/app.min.js", 100, False),
        ("src/huge.ts", 10_000_000, False),
    ],
)
def test_is_indexable(path: str, size: int, expected: bool) -> None:
    assert is_indexable(path, size) is expected


async def test_index_repository_stores_symbols_and_call_edges(worker: WorkerContext) -> None:
    worker.github.set_tree(
        worker.full_name,
        SHA1,
        {
            "src/agenda.ts": AGENDA,
            "src/calendar.ts": CALENDAR,
            "node_modules/x/index.js": "export function bad() {}",
            "README.md": "# hi",
        },
        head_of="main",
    )

    result = await index_repository(worker.deps, worker.repository_id)

    assert result is not None and (result.files, result.symbols) == (2, 3)
    symbols = await worker.symbols()
    assert set(symbols) == {
        ("src/agenda.ts", "validateBooking"),
        ("src/agenda.ts", "unrelated"),
        ("src/calendar.ts", "isFriday"),
    }
    assert all(s.last_seen_sha == SHA1 and s.body_hash for s in symbols.values())
    assert await worker.edges() == {("src/agenda.ts:validateBooking", "src/calendar.ts:isFriday")}
    async with worker.deps.session_factory() as session:
        repo = await session.get(Repository, worker.repository_id)
    assert repo is not None
    assert (repo.full_name, repo.default_branch) == (worker.full_name, "main")


async def test_reindex_retires_deleted_files_and_symbols_and_is_idempotent(
    worker: WorkerContext,
) -> None:
    worker.github.set_tree(
        worker.full_name,
        SHA1,
        {"src/agenda.ts": AGENDA, "src/calendar.ts": CALENDAR},
        head_of="main",
    )
    await index_repository(worker.deps, worker.repository_id)
    await index_repository(worker.deps, worker.repository_id)  # same state again
    assert len(await worker.symbols()) == 3

    worker.github.set_tree(
        worker.full_name,
        SHA2,
        {"src/agenda.ts": AGENDA.replace("export function unrelated() { return 1; }", "")},
        head_of="main",
    )
    await index_repository(worker.deps, worker.repository_id)

    assert set(await worker.symbols()) == {("src/agenda.ts", "validateBooking")}
    assert set(await worker.symbols(status="removed")) == {
        ("src/agenda.ts", "unrelated"),
        ("src/calendar.ts", "isFriday"),
    }
    assert await worker.edges() == set()


async def test_unparsable_or_empty_files_do_not_abort_the_index(worker: WorkerContext) -> None:
    worker.github.set_tree(
        worker.full_name,
        SHA1,
        {"src/ok.ts": "export function fine() { return 1; }", "src/empty.ts": ""},
        head_of="main",
    )

    await index_repository(worker.deps, worker.repository_id)

    assert ("src/ok.ts", "fine") in await worker.symbols()


async def test_repository_without_installation_is_skipped(worker: WorkerContext) -> None:
    async with worker.deps.session_factory() as session, session.begin():
        repo = await session.get(Repository, worker.repository_id)
        assert repo is not None
        repo.installation_id = 0

    assert await index_repository(worker.deps, worker.repository_id) is None
    assert worker.github.calls == []


async def test_analyze_commit_refreshes_changed_files_and_links_unchanged_ones(
    worker: WorkerContext,
) -> None:
    worker.github.set_tree(
        worker.full_name,
        SHA1,
        {"src/agenda.ts": AGENDA, "src/calendar.ts": CALENDAR},
        head_of="main",
    )
    await index_repository(worker.deps, worker.repository_id)
    before = (await worker.symbols())[("src/agenda.ts", "validateBooking")].body_hash

    changed = AGENDA.replace("hour >= 12", "hour >= 13")
    worker.github.set_tree(
        worker.full_name, SHA2, {"src/agenda.ts": changed, "src/calendar.ts": CALENDAR}
    )
    blob = next(
        e.blob_sha
        for e in await worker.github.list_files(77, worker.full_name, SHA2)
        if e.path == "src/agenda.ts"
    )
    worker.github.add_commit(
        worker.full_name, SHA2, [ChangedFile("src/agenda.ts", "modified", blob_sha=blob)]
    )

    summary = await analyze_commit(worker.deps, worker.repository_id, SHA2, True)

    assert summary["files"] == 1
    symbols = await worker.symbols()
    assert symbols[("src/agenda.ts", "validateBooking")].body_hash != before
    assert symbols[("src/agenda.ts", "validateBooking")].last_seen_sha == SHA2
    assert symbols[("src/calendar.ts", "isFriday")].last_seen_sha == SHA1  # untouched file
    assert await worker.edges() == {("src/agenda.ts:validateBooking", "src/calendar.ts:isFriday")}


async def test_analyze_commit_handles_removed_and_renamed_files(worker: WorkerContext) -> None:
    worker.github.set_tree(
        worker.full_name,
        SHA1,
        {"src/agenda.ts": AGENDA, "src/calendar.ts": CALENDAR},
        head_of="main",
    )
    await index_repository(worker.deps, worker.repository_id)

    worker.github.set_tree(worker.full_name, SHA2, {"src/schedule.ts": AGENDA})
    blob = (await worker.github.list_files(77, worker.full_name, SHA2))[0].blob_sha
    worker.github.add_commit(
        worker.full_name,
        SHA2,
        [
            ChangedFile("src/schedule.ts", "renamed", blob_sha=blob, previous_path="src/agenda.ts"),
            ChangedFile("src/calendar.ts", "removed"),
        ],
    )

    await analyze_commit(worker.deps, worker.repository_id, SHA2, True)

    assert {path for path, _ in await worker.symbols()} == {"src/schedule.ts"}
    assert {path for path, _ in await worker.symbols(status="removed")} == {
        "src/agenda.ts",
        "src/calendar.ts",
    }


async def test_analyze_commit_on_feature_branch_only_reports_the_session_join(
    worker: WorkerContext,
) -> None:
    async with worker.deps.session_factory() as session, session.begin():
        session.add(
            AgentSession(
                repository_id=worker.repository_id,
                commit_sha=SHA3,
                session_ref="s1",
                excerpts=[],
            )
        )

    summary = await analyze_commit(worker.deps, worker.repository_id, SHA3, False)

    assert summary["sessions"] == 1
    assert await worker.symbols() == {}
    assert worker.github.calls == []


async def test_analyze_commit_for_unknown_repository_is_a_noop(worker: WorkerContext) -> None:
    import uuid

    assert await analyze_commit(worker.deps, uuid.uuid4(), SHA1, True) == {}
