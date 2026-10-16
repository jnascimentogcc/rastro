"""Thin wrappers around the git executable (always list arguments, never a shell)."""

import subprocess
from datetime import datetime
from pathlib import Path

TIMEOUT_SECONDS = 15


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"git {' '.join(args)} failed: {error}") from error
    if completed.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def repo_root(start: Path) -> Path:
    return Path(run_git(["rev-parse", "--show-toplevel"], start).strip())


def head_sha(root: Path) -> str:
    return run_git(["rev-parse", "HEAD"], root).strip()


def current_branch(root: Path) -> str | None:
    """Branch name, or None on a detached HEAD."""
    name = run_git(["rev-parse", "--abbrev-ref", "HEAD"], root).strip()
    return None if name == "HEAD" else name


def changed_files(root: Path, sha: str) -> list[str]:
    """Files touched by a commit, relative to the repository root, with `/` separators."""
    output = run_git(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", sha], root
    )
    return [name for name in output.split("\0") if name]


def commit_time(root: Path, sha: str) -> datetime:
    return datetime.fromisoformat(run_git(["show", "-s", "--format=%cI", sha], root).strip())


def parent_commit_time(root: Path, sha: str) -> datetime | None:
    try:
        return commit_time(root, f"{sha}^")
    except GitError:
        return None  # root commit


def hooks_dir(root: Path) -> Path:
    path = Path(run_git(["rev-parse", "--git-path", "hooks"], root).strip())
    return path if path.is_absolute() else root / path


def info_exclude_path(root: Path) -> Path:
    path = Path(run_git(["rev-parse", "--git-path", "info/exclude"], root).strip())
    return path if path.is_absolute() else root / path
