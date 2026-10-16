from dataclasses import dataclass
from typing import Literal

ChangeStatus = Literal["added", "modified", "removed", "renamed"]


@dataclass(frozen=True)
class RepoInfo:
    github_id: int
    full_name: str
    default_branch: str


@dataclass(frozen=True)
class FileEntry:
    path: str
    blob_sha: str
    size: int


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: ChangeStatus
    blob_sha: str | None = None  # None for removed files
    previous_path: str | None = None  # set for renames
    patch: str | None = None  # unified diff; None for binary or very large changes
