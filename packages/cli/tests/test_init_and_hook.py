import json
from pathlib import Path

import httpx
import pytest
from cli_support import MemoryKeyring
from typer.testing import CliRunner

from rastro_cli import client
from rastro_cli.config import load_config, load_token
from rastro_cli.hook import BEGIN, install_post_commit
from rastro_cli.main import app

runner = CliRunner()


def serve(monkeypatch: pytest.MonkeyPatch, status: int = 200) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"repository_id": "r", "full_name": "acme/clinic"} if status == 200 else {}
        return httpx.Response(status, json=body)

    monkeypatch.setattr(client, "transport", httpx.MockTransport(handler))
    return seen


def test_init_verifies_token_stores_it_in_keychain_and_installs_hook(
    repo: Path, monkeypatch: pytest.MonkeyPatch, isolated_environment: MemoryKeyring
) -> None:
    monkeypatch.chdir(repo)
    seen = serve(monkeypatch)

    result = runner.invoke(
        app, ["init", "--api-url", "http://rastro.test/", "--token", "rst_secret"]
    )

    assert result.exit_code == 0, result.output
    assert "acme/clinic" in result.output
    assert seen[0].headers["authorization"] == "Bearer rst_secret"
    assert str(seen[0].url) == "http://rastro.test/v1/auth/whoami"

    config = load_config(repo)
    assert config is not None and config.api_url == "http://rastro.test"
    assert "rst_secret" not in (repo / ".rastro" / "config.json").read_text()
    assert load_token(repo) == "rst_secret"
    assert list(isolated_environment.store.values()) == ["rst_secret"]

    hook = (repo / ".git" / "hooks" / "post-commit").read_text()
    assert hook.startswith("#!/bin/sh\n")
    assert "rastro capture" in hook
    assert ".rastro/" in (repo / ".git" / "info" / "exclude").read_text().splitlines()


def test_init_rejects_a_bad_token_without_installing_anything(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    serve(monkeypatch, status=401)

    result = runner.invoke(app, ["init", "--token", "nope"])

    assert result.exit_code == 1
    assert "401" in result.output
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    assert load_config(repo) is None


def test_init_outside_a_repository_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--token", "x", "--skip-verify"])

    assert result.exit_code == 1
    assert "git repository" in result.output


def test_init_is_idempotent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    serve(monkeypatch)

    for _ in range(2):
        assert runner.invoke(app, ["init", "--token", "t"]).exit_code == 0

    hook = (repo / ".git" / "hooks" / "post-commit").read_text()
    assert hook.count(BEGIN) == 1
    assert (repo / ".git" / "info" / "exclude").read_text().count(".rastro/") == 1


def test_existing_shell_hook_is_chained_and_keeps_its_logic(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-commit").write_text("#!/bin/bash\necho existing\nexit 0\n")

    assert install_post_commit(hooks) == "chained"

    lines = (hooks / "post-commit").read_text().splitlines()
    assert lines[0] == "#!/bin/bash"
    assert lines[1] == BEGIN  # before the `exit 0` that would otherwise skip it
    assert "echo existing" in lines
    assert install_post_commit(hooks) == "unchanged"


def test_existing_non_shell_hook_is_left_untouched(tmp_path: Path) -> None:
    from rastro_cli.hook import HookError

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    original = "#!/usr/bin/env python3\nprint('hi')\n"
    (hooks / "post-commit").write_text(original)

    with pytest.raises(HookError):
        install_post_commit(hooks)
    assert (hooks / "post-commit").read_text() == original


def test_flush_without_configuration_explains_what_to_do(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["flush"])

    assert result.exit_code == 1
    assert "rastro init" in result.output


def test_capture_hook_never_fails_and_does_nothing_when_unconfigured(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        "rastro_cli.capture.spawn_background_capture", lambda root, sha: started.append(sha)
    )

    monkeypatch.chdir(tmp_path)  # not a repository
    assert runner.invoke(app, ["capture"]).exit_code == 0
    monkeypatch.chdir(repo)  # a repository, but not initialised (and without commits)
    assert runner.invoke(app, ["capture"]).exit_code == 0

    assert started == []


def test_capture_hook_spawns_the_background_worker_for_head(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from cli_support import commit_file

    from rastro_cli.config import Config, save_config

    sha = commit_file(repo, "a.txt", "a", datetime(2026, 10, 1, tzinfo=UTC))
    save_config(repo, Config(api_url="http://x"))
    started: list[str] = []
    monkeypatch.setattr(
        "rastro_cli.capture.spawn_background_capture", lambda root, sha: started.append(sha)
    )
    monkeypatch.chdir(repo)

    assert runner.invoke(app, ["capture"]).exit_code == 0

    assert started == [sha]
    assert json.loads((repo / ".rastro" / "config.json").read_text()) == {"api_url": "http://x"}
