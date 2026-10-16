from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from cli_support import MemoryKeyring, git

from rastro_cli import client


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """No real keychain, no real server, no leaking environment variables."""
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    for name in ("RASTRO_TOKEN", "RASTRO_API_URL", "RASTRO_CLAUDE_PROJECTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(client, "transport", None)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "clinic"
    path.mkdir()
    git(path, "init", "-b", "main")
    return path
