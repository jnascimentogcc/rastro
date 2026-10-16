"""Static-analysis jobs: index a whole repository, or the files touched by one commit.

Only metadata is stored (symbols, hashes, call edges), never source code (spec section 11).
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from sqlalchemy import delete, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_code_analysis import (
    FileAnalysis,
    Symbol,
    SymbolKind,
    UnsupportedLanguageError,
    analyze_source,
    is_supported,
    resolve_call_graph,
)
from rastro_db.models import AgentSession, Commit, Repository, SymbolCall
from rastro_db.models import Symbol as SymbolRow
from rastro_github import ChangedFile, FileEntry
from rastro_worker.deps import WorkerDeps

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 300_000
DOWNLOAD_CONCURRENCY = 10
_INSERT_CHUNK = 500
_SKIPPED_DIRECTORIES = frozenset(
    {"node_modules", "dist", "build", "out", "coverage", ".next", ".angular", "vendor", ".git"}
)


def is_indexable(path: str, size: int) -> bool:
    """Source files worth analyzing: supported language, not generated/vendored, not huge."""
    if size > MAX_FILE_BYTES or not is_supported(path):
        return False
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if name.endswith((".d.ts", ".min.js", ".bundle.js")):
        return False
    return not any(part in _SKIPPED_DIRECTORIES for part in parts[:-1])


@dataclass(frozen=True)
class IndexResult:
    files: int
    symbols: int
    edges: int
    removed_files: int = 0


async def _download_and_analyze(
    deps: WorkerDeps, installation_id: int, full_name: str, files: Iterable[tuple[str, str]]
) -> list[FileAnalysis]:
    """files: (path, blob_sha). Downloads concurrently, analyzes each file independently."""
    gate = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def one(path: str, blob_sha: str) -> FileAnalysis | None:
        async with gate:
            content = await deps.github.get_blob(installation_id, full_name, blob_sha)
        try:
            return analyze_source(content, path)
        except UnsupportedLanguageError:
            return None
        except Exception:  # one unparsable file must not abort the whole index
            logger.exception("static analysis failed for %s", path)
            return None

    results = await asyncio.gather(*(one(path, sha) for path, sha in files))
    return [r for r in results if r is not None]


# --------------------------------------------------------------------------- persistence


async def persist_analyses(
    session: AsyncSession,
    repository_id: uuid.UUID,
    sha: str,
    analyses: Sequence[FileAnalysis],
    *,
    removed_paths: Iterable[str] = (),
) -> IndexResult:
    """Upsert symbols for `analyses`, retire symbols that disappeared from those files or
    belong to `removed_paths`, and rebuild the outgoing call edges of the analyzed files.

    Edges are resolved against the symbols of the analyzed files plus every other active
    symbol of the repository, so calls into unchanged files are found. Edges from unchanged
    files to symbols that appeared only now are picked up by the next full index.
    """
    removed = set(removed_paths)
    symbol_ids: dict[tuple[str, str], uuid.UUID] = {}

    for chunk in _chunks([(a.file_path, s) for a in analyses for s in a.symbols], _INSERT_CHUNK):
        insert_statement = insert(SymbolRow).values(
            [
                {
                    "repository_id": repository_id,
                    "file_path": path,
                    "qualified_name": s.qualified_name,
                    "kind": s.kind,
                    "body_hash": s.body_hash,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "last_seen_sha": sha,
                    "status": "active",
                }
                for path, s in chunk
            ]
        )
        upsert = insert_statement.on_conflict_do_update(
            index_elements=["repository_id", "file_path", "qualified_name"],
            set_={
                col: insert_statement.excluded[col]
                for col in (
                    "kind",
                    "body_hash",
                    "start_line",
                    "end_line",
                    "last_seen_sha",
                    "status",
                )
            },
        ).returning(SymbolRow.id, SymbolRow.file_path, SymbolRow.qualified_name)
        for row in (await session.execute(upsert)).all():
            symbol_ids[(row.file_path, row.qualified_name)] = row.id

    # symbols that vanished from analyzed files
    for analysis in analyses:
        keep = [s.qualified_name for s in analysis.symbols]
        stale = (
            update(SymbolRow)
            .where(
                SymbolRow.repository_id == repository_id,
                SymbolRow.file_path == analysis.file_path,
                SymbolRow.status == "active",
                SymbolRow.qualified_name.not_in(keep) if keep else true(),
            )
            .values(status="removed", last_seen_sha=sha)
        )
        await session.execute(stale)
    if removed:
        await session.execute(
            update(SymbolRow)
            .where(
                SymbolRow.repository_id == repository_id,
                SymbolRow.file_path.in_(removed),
                SymbolRow.status == "active",
            )
            .values(status="removed", last_seen_sha=sha)
        )

    edges = await _rebuild_edges(session, repository_id, analyses, symbol_ids)
    return IndexResult(
        files=len(analyses), symbols=len(symbol_ids), edges=edges, removed_files=len(removed)
    )


async def _rebuild_edges(
    session: AsyncSession,
    repository_id: uuid.UUID,
    analyses: Sequence[FileAnalysis],
    symbol_ids: dict[tuple[str, str], uuid.UUID],
) -> int:
    if not symbol_ids:
        return 0
    await session.execute(delete(SymbolCall).where(SymbolCall.caller_id.in_(symbol_ids.values())))

    analyzed_paths = {a.file_path for a in analyses}
    others = await session.execute(
        select(SymbolRow.id, SymbolRow.file_path, SymbolRow.qualified_name, SymbolRow.kind).where(
            SymbolRow.repository_id == repository_id,
            SymbolRow.status == "active",
            SymbolRow.file_path.not_in(analyzed_paths),
        )
    )
    all_ids = dict(symbol_ids)
    targets: dict[str, list[Symbol]] = defaultdict(list)
    for symbol_id, path, qualified_name, kind in others.all():
        all_ids[(path, qualified_name)] = symbol_id
        targets[path].append(
            Symbol(
                qualified_name=qualified_name,
                kind=cast(SymbolKind, kind),
                body_hash="",
                start_line=0,
                end_line=0,
            )
        )
    context = [FileAnalysis(path, tuple(symbols)) for path, symbols in targets.items()]

    rows = [
        {"caller_id": all_ids[caller], "callee_id": all_ids[callee]}
        for caller, callee in resolve_call_graph([*analyses, *context])
        if caller in symbol_ids and callee in all_ids
    ]
    for chunk in _chunks(rows, _INSERT_CHUNK):
        await session.execute(insert(SymbolCall).values(chunk).on_conflict_do_nothing())
    return len(rows)


def _chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --------------------------------------------------------------------------- jobs


async def index_repository(deps: WorkerDeps, repository_id: uuid.UUID) -> IndexResult | None:
    """Static analysis (no LLM, to keep costs under control) of the default branch tip."""
    async with deps.session_factory() as session:
        repository = await session.get(Repository, repository_id)
    if repository is None or repository.installation_id == 0:
        logger.warning("skipping index of %s: unknown or not installed", repository_id)
        return None

    info = await deps.github.get_repository(repository.installation_id, repository.full_name)
    head = await deps.github.get_branch_head(
        repository.installation_id, repository.full_name, info.default_branch
    )
    entries: list[FileEntry] = await deps.github.list_files(
        repository.installation_id, repository.full_name, head
    )
    indexable = [e for e in entries if is_indexable(e.path, e.size)]
    analyses = await _download_and_analyze(
        deps,
        repository.installation_id,
        repository.full_name,
        ((e.path, e.blob_sha) for e in indexable),
    )

    async with deps.session_factory() as session, session.begin():
        await session.execute(
            update(Repository)
            .where(Repository.id == repository_id)
            .values(full_name=info.full_name, default_branch=info.default_branch)
        )
        known_paths = {a.file_path for a in analyses}
        previously = (
            await session.scalars(
                select(SymbolRow.file_path)
                .where(SymbolRow.repository_id == repository_id, SymbolRow.status == "active")
                .distinct()
            )
        ).all()
        gone = [path for path in previously if path not in known_paths]
        return await persist_analyses(session, repository_id, head, analyses, removed_paths=gone)


async def analyze_commit(
    deps: WorkerDeps, repository_id: uuid.UUID, sha: str, on_default_branch: bool
) -> dict[str, int]:
    """Join the commit with its captured sessions and refresh symbols of the files it touched.

    Symbols describe the default branch, so commits on other branches only report the join.
    Rule extraction (phase 5) will run from here.
    """
    async with deps.session_factory() as session:
        repository = await session.get(Repository, repository_id)
        if repository is None:
            return {}
        sessions_found = len(
            (
                await session.scalars(
                    select(AgentSession.id).where(
                        AgentSession.repository_id == repository_id,
                        AgentSession.commit_sha == sha,
                    )
                )
            ).all()
        )
        known_commit = await session.scalar(
            select(Commit.id).where(Commit.repository_id == repository_id, Commit.sha == sha)
        )
    summary = {"sessions": sessions_found, "known_commit": int(known_commit is not None)}
    if not on_default_branch or repository.installation_id == 0:
        return summary

    changes: list[ChangedFile] = await deps.github.get_commit_files(
        repository.installation_id, repository.full_name, sha
    )
    removed_paths = [c.path for c in changes if c.status == "removed"]
    removed_paths += [c.previous_path for c in changes if c.previous_path]
    to_analyze = [
        (c.path, c.blob_sha)
        for c in changes
        if c.status != "removed" and c.blob_sha and is_indexable(c.path, 0)
    ]
    analyses = await _download_and_analyze(
        deps, repository.installation_id, repository.full_name, to_analyze
    )
    async with deps.session_factory() as session, session.begin():
        result = await persist_analyses(
            session, repository_id, sha, analyses, removed_paths=removed_paths
        )
    return {**summary, "files": result.files, "symbols": result.symbols, "edges": result.edges}
