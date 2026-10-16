"""In-memory GitHub for tests (no network, no credentials)."""

import hashlib
from dataclasses import dataclass, field

from rastro_github.models import ChangedFile, FileEntry, RepoInfo


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(content, usedforsecurity=False).hexdigest()


@dataclass
class _Repo:
    info: RepoInfo
    heads: dict[str, str] = field(default_factory=dict)  # branch -> commit sha
    trees: dict[str, dict[str, bytes]] = field(default_factory=dict)  # ref -> path -> content
    commits: dict[str, list[ChangedFile]] = field(default_factory=dict)


class FakeGitHub:
    def __init__(self) -> None:
        self._repos: dict[str, _Repo] = {}
        self._blobs: dict[str, bytes] = {}
        self.calls: list[str] = []

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
