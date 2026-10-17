import uuid

import pytest
from sqlalchemy import text

from rastro_db import EMBEDDING_DIMENSIONS, create_engine, create_session_factory
from rastro_db.models import Rule, Symbol

pytestmark = pytest.mark.db


async def test_rule_and_symbol_roundtrip_with_embeddings() -> None:
    """Requires `docker compose up -d` and `alembic upgrade head`."""
    engine = create_engine()
    factory = create_session_factory(engine)
    repo_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO repositories (id, github_id, full_name, installation_id,"
                    " default_branch) VALUES (:id, :gid, 'o/r', 1, 'main')"
                ),
                {"id": repo_id, "gid": repo_id.int % 2**62},
            )
            session.add(
                Rule(
                    repository_id=repo_id,
                    statement="Implants cannot be booked on Friday afternoon",
                    confidence=0.9,
                    embedding=[0.1] * EMBEDDING_DIMENSIONS,
                )
            )
            session.add(
                Symbol(
                    repository_id=repo_id,
                    file_path="a.ts",
                    qualified_name="f",
                    kind="function",
                    body_hash="h",
                    last_seen_sha="abc",
                )
            )
            await session.flush()
            rule = (
                await session.execute(
                    text("SELECT status, rationale FROM rules WHERE repository_id = :id"),
                    {"id": repo_id},
                )
            ).one()  # only this test's rule: the database may hold other repositories
            assert rule.status == "active"
            assert rule.rationale is None
            await session.rollback()
    finally:
        await engine.dispose()
