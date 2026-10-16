"""Keep rule anchors attached to the code as it moves (spec section 7.6).

Static analysis retires symbols that vanish from a file (`status = removed`). A rule anchored to
such a symbol must follow the code when it was only renamed or moved, and must be marked
`orphaned` when the code is really gone. Successors are searched in the order of the spec:
same qualified name, same normalized AST hash, embedding similarity above 0.9.

The job is idempotent and scans the whole repository, so it also repairs anchors when a symbol
is deleted in one commit and re-created in a later one.
"""

import logging
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import ColumnElement, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db.models import Repository, Rule, RuleAnchor
from rastro_db.models import Symbol as SymbolRow
from rastro_worker.deps import WorkerDeps

logger = logging.getLogger(__name__)

EMBEDDING_SIMILARITY = 0.9
_LIVE_RULE_STATUSES = ("active", "orphaned")


@dataclass
class ResyncResult:
    by_name: int = 0
    by_hash: int = 0
    by_embedding: int = 0
    orphaned: int = 0
    reactivated: int = 0

    @property
    def reanchored(self) -> int:
        return self.by_name + self.by_hash + self.by_embedding


async def resync_anchors(deps: WorkerDeps, repository_id: uuid.UUID) -> ResyncResult | None:
    async with deps.session_factory() as session, session.begin():
        if await session.get(Repository, repository_id) is None:
            return None
        result = ResyncResult()

        stale = await session.execute(
            select(SymbolRow)
            .join(RuleAnchor, RuleAnchor.symbol_id == SymbolRow.id)
            .join(Rule, Rule.id == RuleAnchor.rule_id)
            .where(
                SymbolRow.repository_id == repository_id,
                SymbolRow.status == "removed",
                Rule.status.in_(_LIVE_RULE_STATUSES),
            )
            .distinct()
        )
        for old in stale.scalars():
            found = await _find_successor(session, old)
            if found is None:
                continue
            successor, how = found
            await _move_anchors(session, old, successor)
            setattr(result, how, getattr(result, how) + 1)

        result.reactivated = await _reactivate(session, repository_id)
        result.orphaned = await _orphan(session, repository_id)
    if result.reanchored or result.orphaned or result.reactivated:
        logger.info("resync %s: %s", repository_id, result)
    return result


# --------------------------------------------------------------------------- successors


async def _find_successor(session: AsyncSession, old: SymbolRow) -> tuple[SymbolRow, str] | None:
    candidates = (
        SymbolRow.repository_id == old.repository_id,
        SymbolRow.status == "active",
        SymbolRow.id != old.id,
        SymbolRow.kind == old.kind,
    )

    by_name = list(
        await session.scalars(
            select(SymbolRow).where(*candidates, SymbolRow.qualified_name == old.qualified_name)
        )
    )
    if by_name:
        same_code = [s for s in by_name if s.body_hash == old.body_hash]
        return _closest(same_code or by_name, old), "by_name"

    by_hash = list(
        await session.scalars(
            select(SymbolRow).where(*candidates, SymbolRow.body_hash == old.body_hash)
        )
    )
    if by_hash:
        return _closest(by_hash, old), "by_hash"

    if old.embedding is not None:
        nearest = await session.scalar(
            select(SymbolRow)
            .where(
                *candidates,
                SymbolRow.embedding.is_not(None),
                SymbolRow.embedding.cosine_distance(old.embedding) <= 1 - EMBEDDING_SIMILARITY,
            )
            .order_by(SymbolRow.embedding.cosine_distance(old.embedding))
            .limit(1)
        )
        if nearest is not None:
            return nearest, "by_embedding"
    return None


def _closest(candidates: list[SymbolRow], old: SymbolRow) -> SymbolRow:
    """Among equally good successors prefer the one in the same directory, then by path, so the
    choice is deterministic."""
    directory = PurePosixPath(old.file_path).parent
    return min(
        candidates,
        key=lambda s: (
            PurePosixPath(s.file_path).parent != directory,
            s.file_path,
            s.qualified_name,
        ),
    )


# --------------------------------------------------------------------------- re-anchoring


async def _move_anchors(session: AsyncSession, old: SymbolRow, successor: SymbolRow) -> None:
    anchors = (
        (await session.execute(select(RuleAnchor).where(RuleAnchor.symbol_id == old.id)))
        .scalars()
        .all()
    )
    for anchor in anchors:
        already = await session.get(RuleAnchor, (anchor.rule_id, successor.id))
        if already is None:
            await session.execute(
                update(RuleAnchor)
                .where(RuleAnchor.rule_id == anchor.rule_id, RuleAnchor.symbol_id == old.id)
                .values(symbol_id=successor.id)
            )
        else:
            # The successor was already linked (e.g. as a duplicate): keep the stronger role.
            if anchor.role == "primary" and already.role != "primary":
                already.role = "primary"
            await session.delete(anchor)
    if successor.embedding is None and old.embedding is not None:
        successor.embedding = old.embedding  # keep similarity search working for the new symbol
    await session.flush()


async def _reactivate(session: AsyncSession, repository_id: uuid.UUID) -> int:
    result = await session.execute(
        update(Rule)
        .where(
            Rule.repository_id == repository_id,
            Rule.status == "orphaned",
            _has_live_anchor(),
        )
        .values(status="active", updated_at=func.now())
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _orphan(session: AsyncSession, repository_id: uuid.UUID) -> int:
    result = await session.execute(
        update(Rule)
        .where(
            Rule.repository_id == repository_id,
            Rule.status == "active",
            ~_has_live_anchor(),
        )
        .values(status="orphaned", updated_at=func.now())
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _has_live_anchor() -> ColumnElement[bool]:
    return exists().where(
        RuleAnchor.rule_id == Rule.id,
        SymbolRow.id == RuleAnchor.symbol_id,
        SymbolRow.status == "active",
    )
