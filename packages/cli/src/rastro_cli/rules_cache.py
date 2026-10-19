"""The local copy of a repository's rules that the agent hook reads (phase 10).

Standard library only, on purpose: the hook runs before every Read/Edit of the agent, so it must
not import typer, httpx or keyring (together ~1 s of start-up on Windows). The hot modules
(`rules_cache`, `rules_block`, `hook_entry`) also avoid `pathlib`, `datetime`, `contextlib`,
`typing` and `tempfile` at import time, each of which adds tens of milliseconds to a process that
Python starts from scratch for every tool call. Do not add imports to them without measuring.

Paths are plain strings (any `os.PathLike` is accepted).
"""

from __future__ import annotations

import json
import os

TYPE_CHECKING = False  # `typing` is only imported for the type checker
if TYPE_CHECKING:
    from typing import Any

DATA_DIR = ".rastro"
CACHE_DIR = "cache"
RULES_FILE = "rules.json"
MAX_CACHE_BYTES = 32 * 1024 * 1024  # a corrupt or hostile file is not worth parsing


def find_root(start: str | os.PathLike[str]) -> str | None:
    """The nearest directory at or above `start` that has a `.rastro` folder (no git call)."""
    directory = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(directory, DATA_DIR)):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def cache_dir(root: str | os.PathLike[str]) -> str:
    return os.path.join(root, DATA_DIR, CACHE_DIR)


def rules_path(root: str | os.PathLike[str]) -> str:
    return os.path.join(cache_dir(root), RULES_FILE)


def load(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The cached snapshot (`rules`, `head_sha`, `etag`, ...), or None when absent or unusable."""
    path = rules_path(root)
    try:
        if os.stat(path).st_size > MAX_CACHE_BYTES:
            return None
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return None
    return data


def save(root: str | os.PathLike[str], snapshot: dict[str, Any], etag: str) -> None:
    """Write the snapshot atomically (a hook reading mid-write sees the old or the new file)."""
    import tempfile  # only the sync writes; the hook must not pay for this import

    directory = cache_dir(root)
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps({**snapshot, "etag": etag}, ensure_ascii=False)
    handle, temp_name = tempfile.mkstemp(dir=directory, prefix=".rules-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        try:  # noqa: SIM105 (contextlib is not imported on the hook path)
            os.chmod(temp_name, 0o600)  # the rules are business text: owner only (no-op on Windows)
        except OSError:
            pass
        os.replace(temp_name, os.path.join(directory, RULES_FILE))
    except BaseException:
        try:  # noqa: SIM105
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def etag_of(snapshot: dict[str, Any] | None) -> str | None:
    value = snapshot.get("etag") if snapshot else None
    return value if isinstance(value, str) and value else None
