import pytest

from rastro_worker.schema import ensure_schema

pytestmark = pytest.mark.db


async def test_ensure_schema_is_idempotent() -> None:
    await ensure_schema()  # applies on a fresh database, no-op otherwise
    assert await ensure_schema() is False
