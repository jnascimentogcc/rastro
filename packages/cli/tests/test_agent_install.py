import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from rastro_cli import agent_install, client, rules_cache
from rastro_cli.config import Config, save_config, save_token
from rastro_cli.main import app

runner = CliRunner()
PYTHON = "C:/tools/rastro/python.exe"
FOREIGN = {"type": "command", "command": "prettier", "args": ["--write"], "timeout": 5}


def read(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result


def ours(settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        hook
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for hook in group["hooks"]
        if agent_install.is_ours(hook)
    ]


# --------------------------------------------------------------------------- the module


def test_a_fresh_install_writes_the_local_settings_file(tmp_path: Path) -> None:
    result = agent_install.install(tmp_path, shared=False, python=PYTHON)

    settings = read(tmp_path / ".claude" / "settings.local.json")
    assert result == "created"
    assert not (tmp_path / ".claude" / "settings.json").exists()
    [start] = settings["hooks"]["SessionStart"]
    [pre] = settings["hooks"]["PreToolUse"]
    assert start["hooks"][0]["args"] == ["-m", "rastro_cli.hook_entry", "session-start"]
    assert pre["matcher"] == "Read|Edit|Write|MultiEdit"
    assert pre["hooks"][0] == {
        "type": "command",
        "command": PYTHON,
        "args": ["-m", "rastro_cli.hook_entry", "pre-tool"],
        "timeout": 3,
    }


def test_the_shared_install_uses_the_script_not_a_machine_path(tmp_path: Path) -> None:
    agent_install.install(tmp_path, shared=True, python=PYTHON)

    settings = read(tmp_path / ".claude" / "settings.json")
    [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
    assert hook["command"] == "rastro-hook" and hook["args"] == ["pre-tool"]
    assert PYTHON not in json.dumps(settings)


def test_installing_twice_changes_nothing(tmp_path: Path) -> None:
    agent_install.install(tmp_path, shared=False, python=PYTHON)
    path = tmp_path / ".claude" / "settings.local.json"
    first = path.read_text(encoding="utf-8")

    assert agent_install.install(tmp_path, shared=False, python=PYTHON) == "unchanged"
    assert path.read_text(encoding="utf-8") == first


def test_reinstalling_from_another_interpreter_replaces_ours_without_duplicates(
    tmp_path: Path,
) -> None:
    agent_install.install(tmp_path, shared=False, python="C:/old/python.exe")

    result = agent_install.install(tmp_path, shared=False, python=PYTHON)

    settings = read(tmp_path / ".claude" / "settings.local.json")
    assert result == "updated"
    assert [hook["command"] for hook in ours(settings)] == [PYTHON, PYTHON]


def test_other_peoples_hooks_and_settings_survive_install_and_uninstall(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(git status)"]},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Edit", "hooks": [FOREIGN]},
                        {"matcher": "Bash", "hooks": [FOREIGN]},
                    ],
                    "Stop": [{"hooks": [FOREIGN]}],
                },
            }
        ),
        encoding="utf-8",
    )

    agent_install.install(tmp_path, shared=False, python=PYTHON)
    installed = read(path)
    assert installed["permissions"] == {"allow": ["Bash(git status)"]}
    assert installed["hooks"]["Stop"] == [{"hooks": [FOREIGN]}]
    assert [g["matcher"] for g in installed["hooks"]["PreToolUse"]] == [
        "Edit",
        "Bash",
        "Read|Edit|Write|MultiEdit",
    ]

    assert agent_install.uninstall(tmp_path, shared=False) == "removed"
    after = read(path)
    assert ours(after) == []
    assert after["hooks"]["PreToolUse"] == [
        {"matcher": "Edit", "hooks": [FOREIGN]},
        {"matcher": "Bash", "hooks": [FOREIGN]},
    ]
    assert "SessionStart" not in after["hooks"]  # the event we alone used is gone


def test_a_group_that_mixes_our_hook_with_another_keeps_the_other(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    mixed = {
        "matcher": "Read|Edit|Write|MultiEdit",
        "hooks": [
            FOREIGN,
            {
                "type": "command",
                "command": "old",
                "args": ["-m", "rastro_cli.hook_entry", "pre-tool"],
            },
        ],
    }
    path.write_text(json.dumps({"hooks": {"PreToolUse": [mixed]}}), encoding="utf-8")

    agent_install.uninstall(tmp_path, shared=False)

    assert read(path) == {"hooks": {"PreToolUse": [{**mixed, "hooks": [FOREIGN]}]}}


def test_uninstall_deletes_a_file_it_leaves_empty_and_is_idempotent(tmp_path: Path) -> None:
    agent_install.install(tmp_path, shared=False, python=PYTHON)

    assert agent_install.uninstall(tmp_path, shared=False) == "removed"
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert agent_install.uninstall(tmp_path, shared=False) == "nothing"


@pytest.mark.parametrize(
    "content", ["{broken", "[1, 2]", '{"hooks": []}', '{"hooks": {"PreToolUse": 3}}']
)
def test_an_unusable_settings_file_is_refused_and_left_alone(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    with pytest.raises(agent_install.AgentInstallError):
        agent_install.install(tmp_path, shared=False, python=PYTHON)

    assert path.read_text(encoding="utf-8") == content


def test_a_settings_file_with_a_bom_is_read(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": "sonnet"}).encode())

    agent_install.install(tmp_path, shared=False, python=PYTHON)

    assert read(path)["model"] == "sonnet"


# --------------------------------------------------------------------------- the command


@pytest.fixture
def configured(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    save_config(repo, Config(api_url="http://rastro.test", repository_id="r-1"))
    save_token(repo, "rst_token")
    monkeypatch.chdir(repo)
    return repo


def serve(monkeypatch: pytest.MonkeyPatch, rules: int = 2) -> None:
    body = {"head_sha": "abc", "truncated": False, "rules": [{"id": str(i)} for i in range(rules)]}
    monkeypatch.setattr(
        client,
        "transport",
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=body, headers={"ETag": '"e"'})
        ),
    )


def test_install_writes_the_hooks_checks_them_and_downloads_the_rules(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch)

    result = runner.invoke(app, ["agent", "install"])

    assert result.exit_code == 0, result.output
    assert "Claude Code hooks: created (.claude/settings.local.json)." in result.output
    assert "Rules cache updated: 2 rules." in result.output
    assert (
        "could not run" not in result.output
    )  # the real hook module ran once and answered nothing
    [pre] = read(configured / ".claude" / "settings.local.json")["hooks"]["PreToolUse"]
    assert pre["hooks"][0]["command"] == Path(sys.executable).as_posix()
    assert rules_cache.etag_of(rules_cache.load(configured)) == '"e"'


def test_install_still_installs_when_the_server_is_unreachable(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(client, "transport", httpx.MockTransport(down))

    result = runner.invoke(app, ["agent", "install"])

    assert result.exit_code == 0, result.output
    assert (configured / ".claude" / "settings.local.json").exists()
    assert "run `rastro sync` when the server is reachable" in result.output


def test_install_before_init_installs_and_says_what_is_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["agent", "install"])

    assert result.exit_code == 0, result.output
    assert "run `rastro init`" in result.output
    assert (repo / ".claude" / "settings.local.json").exists()


def test_shared_install_warns_that_teammates_need_the_script(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch)

    result = runner.invoke(app, ["agent", "install", "--shared"])

    assert result.exit_code == 0, result.output
    assert (configured / ".claude" / "settings.json").exists()
    assert "rastro-hook" in result.output


def test_uninstall_command_removes_only_what_was_installed(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch)
    runner.invoke(app, ["agent", "install"])

    result = runner.invoke(app, ["agent", "uninstall"])

    assert result.exit_code == 0 and "removed" in result.output
    assert not (configured / ".claude" / "settings.local.json").exists()
    assert "nothing" in runner.invoke(app, ["agent", "uninstall"]).output


def test_a_broken_settings_file_makes_install_fail_without_touching_it(
    configured: Path,
) -> None:
    path = configured / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text("{oops", encoding="utf-8")

    result = runner.invoke(app, ["agent", "install"])

    assert result.exit_code == 1 and "not valid JSON" in result.output
    assert path.read_text(encoding="utf-8") == "{oops"


# --------------------------------------------------------------------------- the MCP server


def mcp_servers(root: Path) -> dict[str, Any]:
    servers: dict[str, Any] = read(root / ".mcp.json")["mcpServers"]
    return servers


def test_the_local_mcp_install_uses_this_interpreter_and_pre_approves_the_server(
    tmp_path: Path,
) -> None:
    result = agent_install.install_mcp(tmp_path, shared=False, python=PYTHON)

    assert result == "created"
    assert mcp_servers(tmp_path) == {
        "rastro": {"type": "stdio", "command": PYTHON, "args": ["-m", "rastro_mcp"]}
    }
    assert read(tmp_path / ".claude" / "settings.local.json")["enabledMcpjsonServers"] == ["rastro"]
    assert agent_install.install_mcp(tmp_path, shared=False, python=PYTHON) == "unchanged"
    assert read(tmp_path / ".claude" / "settings.local.json")["enabledMcpjsonServers"] == ["rastro"]


def test_the_shared_mcp_install_keeps_other_servers_and_asks_nobody_to_pre_approve(
    tmp_path: Path,
) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"github": {"type": "http", "url": "https://x"}}}),
        encoding="utf-8",
    )

    result = agent_install.install_mcp(tmp_path, shared=True, python=PYTHON)

    assert result == "updated"
    assert mcp_servers(tmp_path) == {
        "github": {"type": "http", "url": "https://x"},
        "rastro": {"type": "stdio", "command": "rastro-mcp"},
    }
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_a_local_install_does_not_write_a_machine_path_into_a_team_file(tmp_path: Path) -> None:
    team = {"mcpServers": {"github": {"type": "http", "url": "https://x"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(team), encoding="utf-8")

    with pytest.raises(agent_install.AgentInstallError, match="--shared"):
        agent_install.install_mcp(tmp_path, shared=False, python=PYTHON)

    assert read(tmp_path / ".mcp.json") == team


def test_a_server_called_rastro_that_is_not_ours_is_never_overwritten(tmp_path: Path) -> None:
    mine = {"mcpServers": {"rastro": {"type": "stdio", "command": "something-else"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(mine), encoding="utf-8")

    for shared in (True, False):
        with pytest.raises(agent_install.AgentInstallError, match="not Rastro's"):
            agent_install.install_mcp(tmp_path, shared=shared, python=PYTHON)

    assert read(tmp_path / ".mcp.json") == mine


def test_uninstalling_the_mcp_server_removes_only_ours(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"github": {"type": "http", "url": "https://x"}}}),
        encoding="utf-8",
    )
    agent_install.install_mcp(tmp_path, shared=True, python=PYTHON)

    assert agent_install.uninstall_mcp(tmp_path) == "removed"
    assert mcp_servers(tmp_path) == {"github": {"type": "http", "url": "https://x"}}
    assert agent_install.uninstall_mcp(tmp_path) == "nothing"


def test_uninstalling_a_local_mcp_install_leaves_hooks_and_deletes_empty_files(
    tmp_path: Path,
) -> None:
    agent_install.install(tmp_path, shared=False, python=PYTHON)
    agent_install.install_mcp(tmp_path, shared=False, python=PYTHON)

    assert agent_install.uninstall_mcp(tmp_path) == "removed"

    assert not (tmp_path / ".mcp.json").exists()
    settings = read(tmp_path / ".claude" / "settings.local.json")
    assert "enabledMcpjsonServers" not in settings and len(ours(settings)) == 2  # the hooks stay


def test_install_with_mcp_from_the_command_line(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch)

    result = runner.invoke(app, ["agent", "install", "--mcp"])

    assert result.exit_code == 0, result.output
    assert "MCP server: created (.mcp.json)." in result.output
    assert "mcp__rastro" in result.output  # how to stop the per-call prompts
    assert "cannot import rastro_mcp" not in result.output
    assert mcp_servers(configured)["rastro"]["args"] == ["-m", "rastro_mcp"]
    exclude = (configured / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".mcp.json" in exclude.splitlines()  # a local install is never committed by accident

    removed = runner.invoke(app, ["agent", "uninstall", "--mcp"])

    assert "MCP server: removed" in removed.output
    assert not (configured / ".mcp.json").exists()


def test_install_without_mcp_does_not_touch_mcp_json(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch)

    runner.invoke(app, ["agent", "install"])

    assert not (configured / ".mcp.json").exists()
