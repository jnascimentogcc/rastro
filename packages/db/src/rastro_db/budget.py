"""Monthly LLM token budget per repository (spec section 14), measured on `llm_calls`."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db.models import LlmCall


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def tokens_used_this_month(
    session: AsyncSession, repository_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """Input + output tokens of every model call (extraction, impact, embeddings) of the
    repository since the first of the current UTC month."""
    used = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.input_tokens + LlmCall.output_tokens), 0)).where(
            LlmCall.repository_id == repository_id, LlmCall.created_at >= month_start(now)
        )
    )
    return int(used or 0)


async def budget_exhausted(
    session: AsyncSession, repository_id: uuid.UUID, budget: int | None
) -> bool:
    """True when the repository has used up `budget` tokens this month. None or 0 = unlimited.
    Checked before a job starts, so one job can overshoot by what it spends itself."""
    if not budget:
        return False
    return await tokens_used_this_month(session, repository_id) >= budget
