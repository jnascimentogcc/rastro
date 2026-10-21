"""The code a rule is anchored to, as `{"path::qualified.name": body_hash}`.

A confirmation records it (`rule_reviews.anchor_hashes`) so that `rastro health` can tell when the
code changed afterwards (R5). The key is the path and the qualified name, not the symbol id: the
resync moves an anchor to a successor symbol with another id when code is renamed or moved.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db.models import RuleAnchor
from rastro_db.models import Symbol as SymbolRow


def anchor_key(file_path: str, qualified_name: str) -> str:
    return f"{file_path}::{qualified_name}"


async def current_anchor_hashes(session: AsyncSession, rule_id: uuid.UUID) -> dict[str, str]:
    rows = await session.execute(
        select(SymbolRow.file_path, SymbolRow.qualified_name, SymbolRow.body_hash)
        .join(RuleAnchor, RuleAnchor.symbol_id == SymbolRow.id)
        .where(RuleAnchor.rule_id == rule_id, SymbolRow.status == "active")
    )
    return {anchor_key(path, name): body_hash for path, name, body_hash in rows}
