"""In-memory GitHub for tests (no network, no credentials)."""

import hashlib
from dataclasses import dataclass, field

from rastro_github.models import ChangedFile, CommentNotFoundError, FileEntry, RepoInfo


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(content, usedforsecurity=False).hexdigest()


@dataclass
class _Repo:
    info: RepoInfo
    heads: dict[str, str] = field(default_factory=dict)  # branch -> commit sha
    trees: dict[str, dict[str, bytes]] = field(default_factory=dict)  # ref -> path -> content
    commits: dict[str, list[ChangedFile]] = field(default_factory=dict)
    pulls: dict[int, list[ChangedFile]] = field(default_factory=dict)
    pull_commits: dict[int, list[str]] = field(default_factory=dict)


class FakeGitHub:
    def __init__(self) -> None:
        self._repos: dict[str, _Repo] = {}
        self._blobs: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.comments: dict[int, str] = {}  # comment id -> body
        self.comment_targets: dict[int, int] = {}  # comment id -> pull request number
        self._next_comment_id = 1000
        self.permissions: dict[tuple[str, str], str] = {}  # (repo, login) -> permission

    def add_repo(self, github_id: int, full_name: str, default_branch: str = "main") -> None:
        self._repos[full_name] = _Repo(RepoInfo(github_id, full_name, default_branch))

    def set_tree(
        self, full_name: str, ref: str, files: dict[str, str], *, head_of: str | None = None
    ) -> None:
        """Define the files at `ref` (a commit SHA). `head_of` also makes it a branch tip."""
        repo = self._repos[full_name]
        repo.trees[ref] = {path: text.encode("utf-8") for path, text in files.items()}
        for content in repo.trees[ref].values():
            self._blobs[blob_sha(content)] = content
        if head_of is not None:
            repo.heads[head_of] = ref

    def add_pull_request(
        self,
        full_name: str,
        number: int,
        files: list[ChangedFile],
        blobs: dict[str, str] | None = None,
    ) -> None:
        """Define the files of a pull request; `blobs` maps a path to its head content so the
        blob SHAs in `files` can be fetched with `get_blob`."""
        self._repos[full_name].pulls[number] = files
        for text in (blobs or {}).values():
            self._blobs[blob_sha(text.encode())] = text.encode()

    def set_pull_request_commits(self, full_name: str, number: int, shas: list[str]) -> None:
        """The commits of a pull request, oldest first."""
        self._repos[full_name].pull_commits[number] = shas

    def comments_on(self, number: int) -> list[str]:
        return [body for cid, body in self.comments.items() if self.comment_targets[cid] == number]

    def delete_comment(self, comment_id: int) -> None:
        self.comments.pop(comment_id, None)

    def add_commit(self, full_name: str, sha: str, changes: list[ChangedFile]) -> None:
        self._repos[full_name].commits[sha] = changes

    async def get_repository(self, installation_id: int, full_name: str) -> RepoInfo:
        self.calls.append(f"get_repository {full_name}")
        return self._repos[full_name].info

    async def get_branch_head(self, installation_id: int, full_name: str, branch: str) -> str:
        return self._repos[full_name].heads[branch]

    async def list_files(self, installation_id: int, full_name: str, ref: str) -> list[FileEntry]:
        self.calls.append(f"list_files {full_name}@{ref}")
        tree = self._repos[full_name].trees[ref]
        return [FileEntry(path, blob_sha(content), len(content)) for path, content in tree.items()]

    async def get_blob(self, installation_id: int, full_name: str, blob_sha: str) -> bytes:
        self.calls.append(f"get_blob {blob_sha}")
        return self._blobs[blob_sha]

    async def get_commit_files(
        self, installation_id: int, full_name: str, sha: str
    ) -> list[ChangedFile]:
        return self._repos[full_name].commits[sha]

    async def get_pull_request_files(
        self, installation_id: int, full_name: str, number: int
    ) -> list[ChangedFile]:
        self.calls.append(f"get_pull_request_files {full_name}#{number}")
        return self._repos[full_name].pulls[number]

    async def get_pull_request_commits(
        self, installation_id: int, full_name: str, number: int
    ) -> list[str]:
        self.calls.append(f"get_pull_request_commits {full_name}#{number}")
        return list(self._repos[full_name].pull_commits[number])

    async def create_comment(
        self, installation_id: int, full_name: str, number: int, body: str
    ) -> int:
        self._next_comment_id += 1
        self.comments[self._next_comment_id] = body
        self.comment_targets[self._next_comment_id] = number
        return self._next_comment_id

    async def update_comment(
        self, installation_id: int, full_name: str, comment_id: int, body: str
    ) -> None:
        if comment_id not in self.comments:
            raise CommentNotFoundError(str(comment_id))
        self.comments[comment_id] = body

    def set_permission(self, full_name: str, login: str, permission: str) -> None:
        self.permissions[(full_name, login.lower())] = permission

    async def get_collaborator_permission(
        self, installation_id: int, full_name: str, login: str
    ) -> str:
        return self.permissions.get((full_name, login.lower()), "none")
