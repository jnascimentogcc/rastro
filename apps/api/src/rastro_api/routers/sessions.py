from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert

from rastro_api.deps import CurrentRepository, DbSession
from rastro_api.queueing import enqueue
from rastro_api.schemas import SessionCaptureIn, SessionCaptureOut
from rastro_db.coverage import recompute_session_coverage
from rastro_db.models import AgentSession, Commit

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionCaptureOut, status_code=status.HTTP_201_CREATED)
async def capture_session(
    payload: SessionCaptureIn,
    request: Request,
    repository: CurrentRepository,
    session: DbSession,
    response: Response,
) -> SessionCaptureOut:
    """Store a session capture. Idempotent per (commit, session): the CLI retries pending
    captures, so a repeated upload replaces the previous one instead of failing. A repository
    whose GitHub App installation was removed or suspended answers 403: the CLI keeps the capture
    pending and uploads it once the App is installed again."""
    if repository.status != "active":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Repository is {repository.status}: reinstall or unsuspend the Rastro GitHub App",
        )
    values = {
        "repository_id": repository.id,
        "commit_sha": payload.commit_sha,
        "agent": payload.agent,
        "session_ref": payload.session_ref,
        "agent_version": payload.agent_version,
        "git_branch": payload.git_branch,
        "excerpts": [e.model_dump() for e in payload.excerpts],
        "consulted": [c.model_dump() for c in payload.consulted],
        "hook_active": payload.agent_hook,
    }
    insert_statement = insert(AgentSession).values(**values)
    statement = insert_statement.on_conflict_do_update(  # type: ignore[var-annotated]
        constraint="uq_agent_sessions_commit_session",
        set_={
            k: insert_statement.excluded[k]
            for k in (
                "agent",
                "agent_version",
                "git_branch",
                "excerpts",
                "consulted",
                "hook_active",
            )
        },
    ).returning(AgentSession.id, literal_column("(xmax = 0)").label("created"))
    row = (await session.execute(statement)).one()
    # Judged for the whole session, in the same transaction as the capture (spec D6).
    await recompute_session_coverage(session, repository.id, payload.session_ref)
    commit = (
        await session.execute(
            select(Commit.id, Commit.on_default_branch).where(
                Commit.repository_id == repository.id, Commit.sha == payload.commit_sha
            )
        )
    ).first()
    await session.commit()
    if commit is not None and commit.on_default_branch is not False:
        # The push webhook already arrived and the commit is on the default branch (or not known
        # to be elsewhere): extraction cannot wait for it, so queue it now. Commits on a feature
        # branch wait for the merge, see `rule_extraction`.
        await enqueue(
            request.app.state.queue, "rule_extraction", repository.id, sha=payload.commit_sha
        )
    if not row.created:
        response.status_code = status.HTTP_200_OK
    return SessionCaptureOut(
        id=row.id,
        commit_sha=payload.commit_sha,
        session_ref=payload.session_ref,
        excerpts=len(payload.excerpts),
        created=bool(row.created),
    )
