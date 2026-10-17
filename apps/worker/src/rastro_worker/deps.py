"""Runtime dependencies of the tasks, built lazily so importing the task module is free and
tests can inject fakes with `set_deps`."""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rastro_db import create_engine, create_session_factory
from rastro_db.budget import budget_exhausted
from rastro_db.models import LlmCall
from rastro_github import GitHubClient, GithubKitClient, GitHubSettings
from rastro_llm import LLMProvider, LLMSettings, OpenAIProvider, Usage, UsageSink
from rastro_worker.settings import ImpactSettings

logger = logging.getLogger(__name__)


@dataclass
class WorkerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    github: GitHubClient
    llm: LLMProvider | None = None  # None when no model is configured
    impact: ImpactSettings = field(default_factory=ImpactSettings)
    monthly_token_budget: int | None = None  # per repository; None = unlimited

    async def over_budget(self, repository_id: uuid.UUID) -> bool:
        if not self.monthly_token_budget:
            return False
        async with self.session_factory() as session:
            exhausted = await budget_exhausted(session, repository_id, self.monthly_token_budget)
        if exhausted:
            logger.warning(
                "repository %s used its monthly budget of %d LLM tokens; skipping model work",
                repository_id,
                self.monthly_token_budget,
            )
        return exhausted


def usage_recorder(session_factory: async_sessionmaker[AsyncSession]) -> UsageSink:
    """Persist every model call to `llm_calls` (model, tokens, estimated cost)."""

    async def record(usage: Usage) -> None:
        repository_id = usage.metadata.get("repository_id")
        async with session_factory() as session, session.begin():
            session.add(
                LlmCall(
                    repository_id=uuid.UUID(repository_id) if repository_id else None,
                    purpose=usage.purpose,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                )
            )

    return record


_deps: WorkerDeps | None = None


def get_deps() -> WorkerDeps:
    global _deps
    if _deps is None:
        session_factory = create_session_factory(create_engine())
        llm_settings = LLMSettings()
        _deps = WorkerDeps(
            session_factory=session_factory,
            github=GithubKitClient(GitHubSettings()),
            llm=(
                OpenAIProvider(llm_settings, usage_sink=usage_recorder(session_factory))
                if llm_settings.configured
                else None
            ),
            monthly_token_budget=llm_settings.llm_monthly_token_budget,
        )
    return _deps


def set_deps(deps: WorkerDeps | None) -> None:
    global _deps
    _deps = deps
