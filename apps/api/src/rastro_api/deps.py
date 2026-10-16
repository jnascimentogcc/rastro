from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rastro_db import hash_token
from rastro_db.models import Repository

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_repository(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Repository:
    """Authenticate a request by its per-repository bearer token."""
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing repository token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    repository = await session.scalar(
        select(Repository).where(Repository.api_token_hash == hash_token(credentials.credentials))
    )
    if repository is None:
        raise unauthorized
    return repository


CurrentRepository = Annotated[Repository, Depends(get_repository)]
