from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import procrastinate
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from rastro_api.routers import auth, github, sessions
from rastro_db import create_engine, create_session_factory
from rastro_github import GitHubSettings
from rastro_worker.app import build_app as build_queue


class HealthResponse(BaseModel):
    status: str


def create_app(
    engine: AsyncEngine | None = None,
    *,
    queue: procrastinate.App | None = None,
    github_settings: GitHubSettings | None = None,
) -> FastAPI:
    """Engine, job queue and GitHub settings are injectable for tests; by default they are built
    from DATABASE_URL and the GITHUB_* environment variables."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = engine is None
        db_engine = engine or create_engine()
        app.state.session_factory = create_session_factory(db_engine)
        app.state.github_settings = github_settings or GitHubSettings()
        app.state.queue = queue or build_queue()
        try:
            async with app.state.queue.open_async():
                yield
        finally:
            if owned:
                await db_engine.dispose()

    app = FastAPI(title="Rastro API", version="0.0.0", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(github.router)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app


app = create_app()
