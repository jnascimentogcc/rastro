import base64
from typing import Any

import httpx
from githubkit import AppAuthStrategy, GitHub

from rastro_github.models import ChangedFile, ChangeStatus, FileEntry, RepoInfo
from rastro_github.settings import GitHubSettings

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
        owner, name = self._split(full_name)
        tree = (
            await self._for(installation_id).rest.git.async_get_tree(
                owner, name, ref, recursive="1"
            )
        ).parsed_data
        return [
            FileEntry(path=item.path, blob_sha=item.sha, size=item.size or 0)
            for item in tree.tree
            if item.type == "blob" and item.path is not None and item.sha is not None
        ]

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
