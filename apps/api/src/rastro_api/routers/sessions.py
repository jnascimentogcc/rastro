from fastapi import APIRouter, Response, status
from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert

from rastro_api.deps import CurrentRepository, DbSession
from rastro_api.schemas import SessionCaptureIn, SessionCaptureOut
from rastro_db.models import AgentSession

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionCaptureOut, status_code=status.HTTP_201_CREATED)
async def capture_session(
    payload: SessionCaptureIn,
    repository: CurrentRepository,
    session: DbSession,
    response: Response,
) -> SessionCaptureOut:
    """Store a session capture. Idempotent per (commit, session): the CLI retries pending
    captures, so a repeated upload replaces the previous one instead of failing."""
    values = {
        "repository_id": repository.id,
        "commit_sha": payload.commit_sha,
        "agent": payload.agent,
        "session_ref": payload.session_ref,
        "agent_version": payload.agent_version,
        "git_branch": payload.git_branch,
        "excerpts": [e.model_dump() for e in payload.excerpts],
    }
    insert_statement = insert(AgentSession).values(**values)
    statement = insert_statement.on_conflict_do_update(  # type: ignore[var-annotated]
        constraint="uq_agent_sessions_commit_session",
        set_={
            k: insert_statement.excluded[k]
            for k in ("agent", "agent_version", "git_branch", "excerpts")
        },
    ).returning(AgentSession.id, literal_column("(xmax = 0)").label("created"))
    row = (await session.execute(statement)).one()
    await session.commit()
    if not row.created:
        response.status_code = status.HTTP_200_OK
    return SessionCaptureOut(
        id=row.id,
        commit_sha=payload.commit_sha,
        session_ref=payload.session_ref,
        excerpts=len(payload.excerpts),
        created=bool(row.created),
    )
