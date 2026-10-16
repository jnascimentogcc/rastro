import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import aliased

from rastro_db.models import Symbol, SymbolCall
from rastro_github.testing import FakeGitHub
from rastro_worker.deps import WorkerDeps


@dataclass
class WorkerContext:
    deps: WorkerDeps
    github: FakeGitHub
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
