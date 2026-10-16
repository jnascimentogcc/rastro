import os
import subprocess
from datetime import datetime
from pathlib import Path

from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


def git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = when.isoformat()
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Dev",
            "-c",
            "user.email=dev@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit_file(
    repo: Path, relative: str, content: str, when: datetime, message: str = "change"
) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message, when=when)
    return git(repo, "rev-parse", "HEAD")
