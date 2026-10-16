import uuid
from collections.abc import AsyncIterator

import pytest
from api_support import ApiContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from rastro_api.main import create_app
from rastro_db import create_engine, create_session_factory, generate_token, hash_token
from rastro_db.models import AgentSession, Repository


@pytest.fixture
async def api() -> AsyncIterator[ApiContext]:
    """App wired to the compose Postgres with a throwaway repository (needs migrations applied)."""
    engine = create_engine()
    token = generate_token()
    repository_id = uuid.uuid4()
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        session.add(
            Repository(
                id=repository_id,
                github_id=repository_id.int % 2**62,
                full_name="acme/clinic",
                installation_id=1,
                default_branch="main",
                api_token_hash=hash_token(token),
            )
        )
    app = create_app(engine)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield ApiContext(client, token, repository_id, engine)
    async with factory() as session, session.begin():
        await session.execute(
            delete(AgentSession).where(AgentSession.repository_id == repository_id)
        )
        await session.execute(delete(Repository).where(Repository.id == repository_id))
    await engine.dispose()
