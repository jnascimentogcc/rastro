import uuid
from collections.abc import AsyncIterator

import pytest
from api_support import WEBHOOK_SECRET, ApiContext
from httpx import ASGITransport, AsyncClient
from procrastinate import testing
from pydantic import SecretStr
from sqlalchemy import delete, select

from rastro_api.main import create_app
from rastro_db import create_engine, create_session_factory, generate_token, hash_token
from rastro_db.models import (
    AgentSession,
    Commit,
    HealthSnapshot,
    Repository,
    Rule,
    RuleProposal,
    SessionCoverage,
    Symbol,
)
from rastro_github import GitHubSettings
from rastro_llm import FakeProvider
from rastro_worker.app import build_app


@pytest.fixture
async def api() -> AsyncIterator[ApiContext]:
    """App wired to the compose Postgres (migrations applied) with an in-memory job queue and a
    throwaway repository. Everything created for the test is removed afterwards."""
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
    connector = testing.InMemoryConnector()
    app = create_app(
        engine,
        queue=build_app(connector),
        github_settings=GitHubSettings(github_webhook_secret=SecretStr(WEBHOOK_SECRET)),
        llm=FakeProvider(),
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield ApiContext(client, token, repository_id, engine, connector)
    async with factory() as session, session.begin():
        # webhooks may have created other repositories (ids in the range the tests use)
        created = (
            await session.scalars(
                select(Repository.id).where(Repository.github_id.between(9_100_000, 9_199_999))
            )
        ).all()
        for rid in [repository_id, *created]:
            await session.execute(delete(RuleProposal).where(RuleProposal.repository_id == rid))
            await session.execute(
                delete(SessionCoverage).where(SessionCoverage.repository_id == rid)
            )
            await session.execute(delete(HealthSnapshot).where(HealthSnapshot.repository_id == rid))
            await session.execute(delete(Rule).where(Rule.repository_id == rid))
            await session.execute(delete(Symbol).where(Symbol.repository_id == rid))
            await session.execute(delete(AgentSession).where(AgentSession.repository_id == rid))
            await session.execute(delete(Commit).where(Commit.repository_id == rid))
            await session.execute(delete(Repository).where(Repository.id == rid))
    await engine.dispose()
