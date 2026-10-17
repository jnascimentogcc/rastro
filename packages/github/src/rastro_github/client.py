from typing import Protocol

from rastro_github.models import ChangedFile, FileEntry, RepoInfo


class GitHubClient(Protocol):
    """What the workers need from GitHub. Workers depend on this protocol, never on githubkit,
    so tests use `rastro_github.testing.FakeGitHub`."""

    async def get_repository(self, installation_id: int, full_name: str) -> RepoInfo: ...

    async def get_branch_head(self, installation_id: int, full_name: str, branch: str) -> str:
        """Commit SHA at the tip of `branch`."""
        ...

    async def list_files(self, installation_id: int, full_name: str, ref: str) -> list[FileEntry]:
        """Every file in the tree at `ref` (recursive)."""
        ...

    async def get_blob(self, installation_id: int, full_name: str, blob_sha: str) -> bytes: ...

    async def get_commit_files(
        self, installation_id: int, full_name: str, sha: str
    ) -> list[ChangedFile]: ...

    async def get_pull_request_files(
        self, installation_id: int, full_name: str, number: int
    ) -> list[ChangedFile]:
        """Files of a pull request against its base, with patches and head blob SHAs."""
        ...

    async def create_comment(
        self, installation_id: int, full_name: str, number: int, body: str
    ) -> int:
        """Post a comment on a pull request; returns its id."""
        ...

    async def update_comment(
        self, installation_id: int, full_name: str, comment_id: int, body: str
    ) -> None:
        """Edit a comment in place. Raises CommentNotFoundError if it was deleted."""
        ...
