import asyncio
import base64
import logging
from typing import Any

import httpx
from githubkit import AppAuthStrategy, GitHub
from githubkit.exception import RequestFailed

from rastro_github.models import (
    ChangedFile,
    ChangeStatus,
    CommentNotFoundError,
    FileEntry,
    RepoInfo,
)
from rastro_github.settings import GitHubSettings

logger = logging.getLogger(__name__)

_STATUS_MAP: dict[str, ChangeStatus] = {
    "added": "added",
    "modified": "modified",
    "changed": "modified",
    "removed": "removed",
    "renamed": "renamed",
    "copied": "added",
}
_COMMIT_FILES_PER_PAGE = 100
_COMMIT_FILES_MAX_PAGES = 30  # GitHub caps a commit at 3000 files
_PR_FILES_MAX_PAGES = 30  # and a pull request at 3000 files
_PR_COMMITS_PER_PAGE = 100
_PR_COMMITS_MAX_PAGES = 10  # GitHub lists at most 250 commits of a pull request
_TREE_CONCURRENCY = 8


class GithubKitClient:
    """`GitHubClient` implemented with githubkit, authenticating as the GitHub App installation."""

    def __init__(
        self, settings: GitHubSettings, *, async_transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._app: GitHub[Any] = GitHub(
            AppAuthStrategy(settings.require_app_id(), settings.private_key()),
            base_url=settings.github_api_url,
            async_transport=async_transport,
            http_cache=False,
        )
        self._installations: dict[int, GitHub[Any]] = {}

    def _for(self, installation_id: int) -> GitHub[Any]:
        if installation_id not in self._installations:
            self._installations[installation_id] = self._app.with_auth(
                self._app.auth.as_installation(installation_id)
            )
        return self._installations[installation_id]

    @staticmethod
    def _split(full_name: str) -> tuple[str, str]:
        owner, _, name = full_name.partition("/")
        return owner, name

    async def get_repository(self, installation_id: int, full_name: str) -> RepoInfo:
        owner, name = self._split(full_name)
        data = (await self._for(installation_id).rest.repos.async_get(owner, name)).parsed_data
        return RepoInfo(
            github_id=data.id, full_name=data.full_name, default_branch=data.default_branch
        )

    async def get_branch_head(self, installation_id: int, full_name: str, branch: str) -> str:
        owner, name = self._split(full_name)
        data = (
            await self._for(installation_id).rest.repos.async_get_branch(owner, name, branch)
        ).parsed_data
        return data.commit.sha

    async def list_files(self, installation_id: int, full_name: str, ref: str) -> list[FileEntry]:
        """Every file at `ref`. One recursive call covers most repositories; GitHub cuts the
        answer at 100,000 entries / 7 MB and sets `truncated`, in which case the tree is walked
        one directory per call so that nothing is silently missing."""
        owner, name = self._split(full_name)
        github = self._for(installation_id)
        tree = (await github.rest.git.async_get_tree(owner, name, ref, recursive="1")).parsed_data
        if not tree.truncated:
            return _blobs(tree.tree, prefix="")
        logger.warning(
            "tree of %s@%s is truncated by GitHub; listing it directory by directory",
            full_name,
            ref,
        )
        return await self._walk_tree(github, owner, name, ref)

    async def _walk_tree(
        self, github: GitHub[Any], owner: str, name: str, root: str
    ) -> list[FileEntry]:
        gate = asyncio.Semaphore(_TREE_CONCURRENCY)

        async def one(sha: str, prefix: str) -> list[FileEntry]:
            async with gate:
                tree = (await github.rest.git.async_get_tree(owner, name, sha)).parsed_data
            if tree.truncated:  # a single directory with 100k+ entries: keep what we got
                logger.warning("directory %r is still truncated; some files are missing", prefix)
            subtrees = [
                one(item.sha, f"{prefix}{item.path}/")
                for item in tree.tree
                if item.type == "tree" and item.path is not None and item.sha is not None
            ]
            nested = await asyncio.gather(*subtrees)
            return [
                *_blobs(tree.tree, prefix=prefix),
                *(entry for part in nested for entry in part),
            ]

        return await one(root, "")

    async def get_blob(self, installation_id: int, full_name: str, blob_sha: str) -> bytes:
        owner, name = self._split(full_name)
        blob = (
            await self._for(installation_id).rest.git.async_get_blob(owner, name, blob_sha)
        ).parsed_data
        if blob.encoding == "base64":
            return base64.b64decode(blob.content)
        return blob.content.encode("utf-8")

    async def get_commit_files(
        self, installation_id: int, full_name: str, sha: str
    ) -> list[ChangedFile]:
        owner, name = self._split(full_name)
        github = self._for(installation_id)
        changed: list[ChangedFile] = []
        for page in range(1, _COMMIT_FILES_MAX_PAGES + 1):
            commit = (
                await github.rest.repos.async_get_commit(
                    owner, name, sha, page=page, per_page=_COMMIT_FILES_PER_PAGE
                )
            ).parsed_data
            files = commit.files or []
            for item in files:
                status = _STATUS_MAP.get(item.status, "modified")
                changed.append(
                    ChangedFile(
                        path=item.filename,
                        status=status,
                        blob_sha=None if status == "removed" else item.sha,
                        previous_path=item.previous_filename or None,
                        patch=item.patch or None,
                    )
                )
            if len(files) < _COMMIT_FILES_PER_PAGE:
                break
        return changed

    async def get_pull_request_files(
        self, installation_id: int, full_name: str, number: int
    ) -> list[ChangedFile]:
        owner, name = self._split(full_name)
        github = self._for(installation_id)
        changed: list[ChangedFile] = []
        for page in range(1, _PR_FILES_MAX_PAGES + 1):
            files: list[Any] = list(
                (
                    await github.rest.pulls.async_list_files(
                        owner, name, number, per_page=_COMMIT_FILES_PER_PAGE, page=page
                    )
                ).parsed_data
            )
            for item in files:
                status = _STATUS_MAP.get(item.status, "modified")
                changed.append(
                    ChangedFile(
                        path=item.filename,
                        status=status,
                        blob_sha=None if status == "removed" else item.sha,
                        previous_path=item.previous_filename or None,
                        patch=item.patch or None,
                    )
                )
            if len(files) < _COMMIT_FILES_PER_PAGE:
                break
        return changed

    async def get_pull_request_commits(
        self, installation_id: int, full_name: str, number: int
    ) -> list[str]:
        owner, name = self._split(full_name)
        github = self._for(installation_id)
        shas: list[str] = []
        for page in range(1, _PR_COMMITS_MAX_PAGES + 1):
            commits: list[Any] = list(
                (
                    await github.rest.pulls.async_list_commits(
                        owner, name, number, per_page=_PR_COMMITS_PER_PAGE, page=page
                    )
                ).parsed_data
            )
            shas.extend(item.sha for item in commits)
            if len(commits) < _PR_COMMITS_PER_PAGE:
                break
        return shas

    async def create_comment(
        self, installation_id: int, full_name: str, number: int, body: str
    ) -> int:
        owner, name = self._split(full_name)
        created = (
            await self._for(installation_id).rest.issues.async_create_comment(
                owner, name, number, body=body
            )
        ).parsed_data
        return created.id

    async def update_comment(
        self, installation_id: int, full_name: str, comment_id: int, body: str
    ) -> None:
        owner, name = self._split(full_name)
        try:
            await self._for(installation_id).rest.issues.async_update_comment(
                owner, name, comment_id, body=body
            )
        except RequestFailed as error:
            if error.response.status_code == 404:
                raise CommentNotFoundError(str(comment_id)) from error
            raise

    async def get_collaborator_permission(
        self, installation_id: int, full_name: str, login: str
    ) -> str:
        owner, name = self._split(full_name)
        try:
            response = await self._for(
                installation_id
            ).rest.repos.async_get_collaborator_permission_level(owner, name, login)
        except RequestFailed as error:
            if error.response.status_code in (403, 404):  # not a collaborator, or not a user
                return "none"
            raise
        return str(response.parsed_data.permission)


def _blobs(items: list[Any], *, prefix: str) -> list[FileEntry]:
    return [
        FileEntry(path=f"{prefix}{item.path}", blob_sha=item.sha, size=item.size or 0)
        for item in items
        if item.type == "blob" and item.path is not None and item.sha is not None
    ]
