from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from rastro_api.routers import auth, sessions
from rastro_db import create_engine, create_session_factory


class HealthResponse(BaseModel):
    status: str


def create_app(engine: AsyncEngine | None = None) -> FastAPI:
    """`engine` is injectable for tests; by default it is built from DATABASE_URL."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = engine is None
        db_engine = engine or create_engine()
        app.state.session_factory = create_session_factory(db_engine)
        try:
            yield
        finally:
            if owned:
                await db_engine.dispose()

    app = FastAPI(title="Rastro API", version="0.0.0", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(sessions.router)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app


app = create_app()
