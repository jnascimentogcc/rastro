import difflib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import aliased

from rastro_db.models import (
    AgentSession,
    Commit,
    PrAnalysis,
    Rule,
    RuleAnchor,
    RuleOrigin,
    Symbol,
    SymbolCall,
)
from rastro_github import ChangedFile
from rastro_github.testing import FakeGitHub, blob_sha
from rastro_llm import FakeProvider
from rastro_worker.deps import WorkerDeps


def make_patch(old: str, new: str) -> str:
    """Unified diff of two file versions in the shape GitHub returns (hunks only)."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    return "\n".join(list(lines)[2:])


@dataclass
class WorkerContext:
    deps: WorkerDeps
    github: FakeGitHub
    llm: FakeProvider
    repository_id: uuid.UUID
    full_name: str
    engine: AsyncEngine

    async def edges(self) -> set[tuple[str, str]]:
        """Call edges as ("file:caller", "file:callee")."""
        caller, callee = aliased(Symbol), aliased(Symbol)
        statement = (
            select(caller.file_path, caller.qualified_name, callee.file_path, callee.qualified_name)
            .select_from(SymbolCall)
            .join(caller, caller.id == SymbolCall.caller_id)
            .join(callee, callee.id == SymbolCall.callee_id)
            .where(caller.repository_id == self.repository_id)
        )
        async with self.deps.session_factory() as session:
            rows = (await session.execute(statement)).all()
        return {(f"{a}:{b}", f"{c}:{d}") for a, b, c, d in rows}

    async def symbols(self, *, status: str = "active") -> dict[tuple[str, str], Symbol]:
        async with self.deps.session_factory() as session:
            rows = await session.scalars(
                select(Symbol).where(
                    Symbol.repository_id == self.repository_id, Symbol.status == status
                )
            )
            return {(s.file_path, s.qualified_name): s for s in rows}

    async def rules(self) -> list[Rule]:
        async with self.deps.session_factory() as session:
            return list(
                await session.scalars(
                    select(Rule)
                    .where(Rule.repository_id == self.repository_id)
                    .order_by(Rule.created_at)
                )
            )

    async def anchors(self, rule_id: uuid.UUID) -> dict[tuple[str, str], str]:
        """(file, symbol) -> role for one rule."""
        async with self.deps.session_factory() as session:
            rows = await session.execute(
                select(Symbol.file_path, Symbol.qualified_name, RuleAnchor.role)
                .join(RuleAnchor, RuleAnchor.symbol_id == Symbol.id)
                .where(RuleAnchor.rule_id == rule_id)
            )
            return {(path, name): role for path, name, role in rows}

    async def add_rule(
        self,
        statement: str,
        anchors: dict[tuple[str, str], str],
        *,
        status: str = "active",
        rationale: str | None = None,
    ) -> uuid.UUID:
        """A rule anchored to existing symbols: {(file, qualified_name): role}."""
        async with self.deps.session_factory() as session, session.begin():
            rule = Rule(
                repository_id=self.repository_id,
                statement=statement,
                rationale=rationale,
                confidence=0.9,
                status=status,
            )
            session.add(rule)
            await session.flush()
            for (path, name), role in anchors.items():
                symbol_id = await session.scalar(
                    select(Symbol.id).where(
                        Symbol.repository_id == self.repository_id,
                        Symbol.file_path == path,
                        Symbol.qualified_name == name,
                    )
                )
                assert symbol_id is not None, f"symbol {path}::{name} is not indexed"
                session.add(RuleAnchor(rule_id=rule.id, symbol_id=symbol_id, role=role))
            return rule.id

    async def add_origin(self, rule_id: uuid.UUID, sha: str, committed_at: datetime) -> None:
        async with self.deps.session_factory() as session, session.begin():
            session.add(
                Commit(repository_id=self.repository_id, sha=sha, committed_at=committed_at)
            )
            session.add(RuleOrigin(rule_id=rule_id, commit_sha=sha))

    async def rule_status(self, rule_id: uuid.UUID) -> str:
        async with self.deps.session_factory() as session:
            status = await session.scalar(select(Rule.status).where(Rule.id == rule_id))
            assert status is not None
            return status

    async def set_embedding(self, path: str, name: str, vector: list[float]) -> None:
        async with self.deps.session_factory() as session, session.begin():
            symbol = await session.scalar(
                select(Symbol).where(
                    Symbol.repository_id == self.repository_id,
                    Symbol.file_path == path,
                    Symbol.qualified_name == name,
                )
            )
            assert symbol is not None
            symbol.embedding = vector

    async def embedding_of(self, path: str, name: str) -> list[float] | None:
        async with self.deps.session_factory() as session:
            return await session.scalar(
                select(Symbol.embedding).where(
                    Symbol.repository_id == self.repository_id,
                    Symbol.file_path == path,
                    Symbol.qualified_name == name,
                )
            )

    async def pr_analysis(self, number: int) -> PrAnalysis | None:
        async with self.deps.session_factory() as session:
            return await session.scalar(
                select(PrAnalysis).where(
                    PrAnalysis.repository_id == self.repository_id, PrAnalysis.pr_number == number
                )
            )

    def stage_pull_request(
        self, number: int, files: dict[str, str], changes: dict[str, str | None]
    ) -> None:
        """A pull request whose head has `files`; `changes` maps path -> old content (None = new)."""
        self.github.add_pull_request(
            self.full_name,
            number,
            [
                ChangedFile(
                    path,
                    "added" if old is None else "modified",
                    blob_sha=blob_sha(files[path].encode()),
                    patch=make_patch(old or "", files[path]),
                )
                for path, old in changes.items()
            ],
            blobs=files,
        )

    async def commit(self, sha: str) -> Commit | None:
        async with self.deps.session_factory() as session:
            return await session.scalar(
                select(Commit).where(Commit.repository_id == self.repository_id, Commit.sha == sha)
            )

    async def add_session(self, sha: str, excerpts: list[dict[str, object]]) -> uuid.UUID:
        async with self.deps.session_factory() as session, session.begin():
            row = AgentSession(
                repository_id=self.repository_id,
                commit_sha=sha,
                session_ref=f"s-{uuid.uuid4().hex[:6]}",
                excerpts=excerpts,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def add_commit_row(self, sha: str, message: str = "change") -> None:
        async with self.deps.session_factory() as session, session.begin():
            session.add(Commit(repository_id=self.repository_id, sha=sha, message=message))

    def stage_commit(self, sha: str, files: dict[str, str], changes: dict[str, str | None]) -> None:
        """Define the tree at `sha` and which files it modified. `changes` maps a path to the old
        content (None = new file); the diff is computed against the new content in `files`."""
        self.github.set_tree(self.full_name, sha, files)
        self.github.add_commit(
            self.full_name,
            sha,
            [
                ChangedFile(
                    path,
                    "added" if old is None else "modified",
                    blob_sha=blob_sha(files[path].encode()),
                    patch=make_patch(old or "", files[path]),
                )
                for path, old in changes.items()
            ],
        )


def user_excerpt(text: str, kind: str = "intent") -> dict[str, object]:
    return {
        "role": "user",
        "kind": kind,
        "text": text,
        "timestamp": "2026-10-01T10:00:00.000Z",
        "files": [],
    }
