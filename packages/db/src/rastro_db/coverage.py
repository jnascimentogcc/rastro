"""Which files with rules a session edited, and whether the agent hook announced them first.

Computed when a capture arrives (spec D6), for the whole session: the announcements travel with the
first capture of a session, so the later captures of a session with several commits carry none, and
judging each capture alone would show 0% for them. The edits are in the encrypted excerpts; reading
them here, once, keeps `rastro health` fast and leaves the conversation text where it is.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db.models import AgentSession, Rule, RuleAnchor, SessionCoverage
from rastro_db.models import Symbol as SymbolRow


def parse_time(value: object) -> datetime | None:
    """ISO 8601 as the agent and the hook write it (`...Z`), as an aware UTC time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _same(left: str, right: str) -> bool:
    return left.replace("\\", "/").lower() == right.replace("\\", "/").lower()


async def recompute_session_coverage(
    session: AsyncSession, repository_id: uuid.UUID, session_ref: str
) -> None:
    rows = list(
        await session.scalars(
            select(AgentSession).where(
                AgentSession.repository_id == repository_id, AgentSession.session_ref == session_ref
            )
        )
    )
    if not rows:
        return
    first_edit: dict[str, datetime] = {}
    started: datetime | None = None
    for row in rows:
        for excerpt in row.excerpts:
            moment = parse_time(excerpt.get("timestamp"))
            if moment is None:
                continue
            started = moment if started is None or moment < started else started
            if excerpt.get("kind") != "edit":
                continue
            for path in excerpt.get("files") or []:
                if path not in first_edit or moment < first_edit[path]:
                    first_edit[path] = moment

    announced: list[tuple[str, datetime]] = []
    for row in rows:
        for entry in row.consulted or []:
            moment = parse_time(entry.get("at"))
            if moment is not None and entry.get("via") in ("read", "deny") and entry.get("file"):
                announced.append((str(entry["file"]), moment))

    files: list[dict[str, Any]] = []
    for path, edited_at in sorted(first_edit.items()):
        had_rule = await session.scalar(
            select(func.count())
            .select_from(Rule)
            .join(RuleAnchor, RuleAnchor.rule_id == Rule.id)
            .join(SymbolRow, SymbolRow.id == RuleAnchor.symbol_id)
            .where(
                Rule.repository_id == repository_id,
                Rule.status == "active",
                Rule.created_at < edited_at,
                SymbolRow.file_path == path,
                SymbolRow.status == "active",
            )
        )
        if not had_rule:
            continue
        files.append(
            {
                "file": path,
                "consulted": any(_same(seen, path) and at <= edited_at for seen, at in announced),
            }
        )

    flags = [row.hook_active for row in rows if row.hook_active is not None]
    hook_active: bool | None = True if True in flags else (False if flags else None)
    values = {
        "started_at": started,
        "computed_at": datetime.now(UTC),
        "hook_active": hook_active,
        "edited_ruled": len(files),
        "consulted_first": sum(1 for item in files if item["consulted"]),
        "files": files,
    }
    statement = insert(SessionCoverage).values(
        repository_id=repository_id, session_ref=session_ref, **values
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["repository_id", "session_ref"], set_=values
        )
    )
