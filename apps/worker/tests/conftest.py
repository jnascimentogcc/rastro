import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from worker_support import WorkerContext

from rastro_db import create_engine, create_session_factory
from rastro_db.models import AgentSession, Commit, LlmCall, PrAnalysis, Repository, Rule, Symbol
from rastro_github.testing import FakeGitHub
from rastro_llm import FakeProvider
from rastro_worker.deps import WorkerDeps, set_deps, usage_recorder


@pytest.fixture
async def worker() -> AsyncIterator[WorkerContext]:
    """Worker dependencies wired to the compose Postgres, an in-memory GitHub and a fake LLM,
    with a throwaway installed repository (needs migrations applied)."""
    engine = create_engine()
    factory = create_session_factory(engine)
    repository_id = uuid.uuid4()
    github_id = 9_200_000 + repository_id.int % 99_999
    full_name = f"acme/worker-{repository_id.hex[:8]}"
    github = FakeGitHub()
    github.add_repo(github_id, full_name, default_branch="main")
    async with factory() as session, session.begin():
        session.add(
            Repository(
                id=repository_id,
                github_id=github_id,
                full_name=full_name,
                installation_id=77,
                default_branch="master",
            )
        )
    llm = FakeProvider(usage_sink=usage_recorder(factory))
    deps = WorkerDeps(session_factory=factory, github=github, llm=llm)
    set_deps(deps)
    yield WorkerContext(deps, github, llm, repository_id, full_name, engine)
    set_deps(None)
    async with factory() as session, session.begin():
        await session.execute(delete(LlmCall).where(LlmCall.repository_id == repository_id))
        await session.execute(delete(PrAnalysis).where(PrAnalysis.repository_id == repository_id))
        await session.execute(delete(Rule).where(Rule.repository_id == repository_id))
        await session.execute(delete(Symbol).where(Symbol.repository_id == repository_id))
        await session.execute(
            delete(AgentSession).where(AgentSession.repository_id == repository_id)
        )
        await session.execute(delete(Commit).where(Commit.repository_id == repository_id))
        await session.execute(delete(Repository).where(Repository.id == repository_id))
    await engine.dispose()
