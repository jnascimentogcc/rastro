"""Runtime dependencies of the tasks, built lazily so importing the task module is free and
tests can inject fakes with `set_deps`."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rastro_db import create_engine, create_session_factory
from rastro_github import GitHubClient, GithubKitClient, GitHubSettings


@dataclass
class WorkerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    github: GitHubClient


_deps: WorkerDeps | None = None


def get_deps() -> WorkerDeps:
    global _deps
    if _deps is None:
        _deps = WorkerDeps(
            session_factory=create_session_factory(create_engine()),
            github=GithubKitClient(GitHubSettings()),
        )
    return _deps


def set_deps(deps: WorkerDeps | None) -> None:
    global _deps
    _deps = deps
