"""A merged pull request releases the sessions of its own commits.

A merge commit pushes the PR's commits to the default branch again, so the push webhook flips
them. A squash or rebase merge writes NEW commits instead; the sessions stay stored under the
SHAs the developer machine saw. So on `pull_request` `closed` + `merged` we list the PR's commits
through the GitHub API, mark them as default-branch commits and let `rule_extraction` run on each
one's own diff (`get_commit_files` at the original SHA keeps working while the PR refs exist).
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import false, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from rastro_db.models import AgentSession, Commit, Repository
from rastro_worker.deps import WorkerDeps
from rastro_worker.preview import set_pr_author

logger = logging.getLogger(__name__)


@dataclass
class MergedPullRequest:
    commit_shas: list[str]  # every commit of the PR, oldest first
    extract_shas: list[str]  # the ones that have a captured session, same order


async def release_pull_request_commits(
    deps: WorkerDeps,
    repository_id: uuid.UUID,
    number: int,
    *,
    merge_commit_sha: str | None = None,
    author: str | None = None,
) -> MergedPullRequest:
    """Mark the PR's commits as default-branch commits (creating rows for unknown ones) and say
    which of them have sessions to extract rules from. Idempotent."""
    async with deps.session_factory() as session:
        repository = await session.get(Repository, repository_id)
    if repository is None or repository.installation_id == 0:
        return MergedPullRequest([], [])
    shas = list(
        dict.fromkeys(
            await deps.github.get_pull_request_commits(
                repository.installation_id, repository.full_name, number
            )
        )
    )
    if not shas:
        return MergedPullRequest([], [])

    async with deps.session_factory() as session, session.begin():
        # The merge or squash commit belongs to the PR too (it carries no session of its own).
        members = [
            *shas,
            *([merge_commit_sha] if merge_commit_sha and merge_commit_sha not in shas else []),
        ]
        statement = insert(Commit).values(
            [
                {
                    "repository_id": repository_id,
                    "sha": sha,
                    "on_default_branch": True,
                    "merged_pr_number": number,
                }
                for sha in members
            ]
        )
        # Same rule as the push webhook: the flag only ever goes from False/NULL to True.
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["repository_id", "sha"],
                set_={
                    "on_default_branch": or_(
                        func.coalesce(Commit.on_default_branch, false()),
                        statement.excluded.on_default_branch,
                    ),
                    # a commit keeps the PR it was first recorded with
                    "merged_pr_number": func.coalesce(
                        Commit.merged_pr_number, statement.excluded.merged_pr_number
                    ),
                },
            )
        )
        with_session = set(
            await session.scalars(
                select(AgentSession.commit_sha).where(
                    AgentSession.repository_id == repository_id, AgentSession.commit_sha.in_(shas)
                )
            )
        )
    await set_pr_author(deps, repository_id, number, author)
    logger.info(
        "pull request %s#%d merged: %d commits, %d with sessions",
        repository.full_name,
        number,
        len(shas),
        len(with_session),
    )
    return MergedPullRequest(shas, [sha for sha in shas if sha in with_session])
