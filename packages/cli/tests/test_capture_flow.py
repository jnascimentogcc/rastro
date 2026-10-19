import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cli_support import commit_file
from typer.testing import CliRunner

from rastro_cli import client
from rastro_cli.capture import run_capture
from rastro_cli.config import Config, pending_dir, rejected_dir, save_config, save_token
from rastro_cli.main import app
from rastro_session_parsers.testing import SessionBuilder

runner = CliRunner()
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def scenario(repo: Path, tmp_path: Path) -> tuple[Path, str, Path]:
    """Repo with two commits and a Claude Code session that edited src/agenda.ts in between."""
    commit_file(repo, "README.md", "hello", T0, "initial")
    builder = SessionBuilder(str(repo), session_id="sess-42", start=T0 + timedelta(minutes=1))
    builder.human(
        "Implants cannot be booked on Friday afternoon. Key: sk-abcdefghijklmnopqrstuvwxyz1234"
    )
    builder.assistant_text("Editing src/agenda.ts now")
    builder.tool("Edit", {"file_path": str(repo / "src" / "agenda.ts")}, tool_id="t1")
    projects = tmp_path / "claude" / "projects"
    builder.write(projects / "encoded-name" / "sess-42.jsonl")
    sha = commit_file(repo, "src/agenda.ts", "export const x = 1;", T0 + timedelta(minutes=10))
    save_config(repo, Config(api_url="http://rastro.test"))
    save_token(repo, "rst_token")
    return repo, sha, projects


def serve(monkeypatch: pytest.MonkeyPatch, status: int = 201) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={})

    monkeypatch.setattr(client, "transport", httpx.MockTransport(handler))
    return seen


def test_capture_run_uploads_the_session_linked_to_the_commit(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://rastro.test/v1/sessions"
    assert request.headers["authorization"] == "Bearer rst_token"
    body = json.loads(request.content)
    assert body["commit_sha"] == sha
    assert body["session_ref"] == "sess-42"
    assert body["git_branch"] == "main"
    kinds = {e["kind"] for e in body["excerpts"]}
    assert {"intent", "edit", "assistant_note"} <= kinds
    assert "Friday afternoon" in json.dumps(body)
    assert "sk-abcdef" not in json.dumps(body)
    assert "SECRET FILE CONTENT" not in json.dumps(body)
    assert list(pending_dir(repo).glob("*.json")) == []


def test_failed_upload_stays_pending_and_flush_retries_it(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    serve(monkeypatch, status=503)

    run_capture(repo, sha, projects)

    assert len(list(pending_dir(repo).glob("*.json"))) == 1

    seen = serve(monkeypatch, status=201)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["flush"])

    assert result.exit_code == 0, result.output
    assert "Sent 1" in result.output
    assert len(seen) == 1
    assert list(pending_dir(repo).glob("*.json")) == []


def test_network_error_keeps_the_capture(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(client, "transport", httpx.MockTransport(boom))

    run_capture(repo, sha, projects)  # must not raise

    assert len(list(pending_dir(repo).glob("*.json"))) == 1
    assert "upload failed" in (repo / ".rastro" / "capture.log").read_text()


def test_unauthorized_keeps_the_capture(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    serve(monkeypatch, status=401)

    run_capture(repo, sha, projects)

    assert len(list(pending_dir(repo).glob("*.json"))) == 1
    assert not rejected_dir(repo).exists()


def test_rejected_payload_is_moved_aside_instead_of_retried_forever(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    serve(monkeypatch, status=422)

    run_capture(repo, sha, projects)

    assert list(pending_dir(repo).glob("*.json")) == []
    assert len(list(rejected_dir(repo).glob("*.json"))) == 1


def test_commit_without_matching_session_sends_nothing(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, projects = scenario
    other = commit_file(repo, "src/unrelated.ts", "export {};", T0 + timedelta(minutes=20))
    seen = serve(monkeypatch)

    run_capture(repo, other, projects)

    assert seen == []
    assert not pending_dir(repo).exists() or list(pending_dir(repo).glob("*.json")) == []


def test_unconfigured_repository_is_a_silent_noop(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = commit_file(repo, "a.txt", "a", T0)
    seen = serve(monkeypatch)

    run_capture(repo, sha, tmp_path)

    assert seen == []
    assert not (repo / ".rastro").exists()


def test_capture_run_command_reads_projects_dir_from_environment(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    seen = serve(monkeypatch)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("RASTRO_CLAUDE_PROJECTS_DIR", str(projects))

    result = runner.invoke(app, ["capture-run", "--sha", sha])

    assert result.exit_code == 0
    assert len(seen) == 1


def test_silent_capture_is_explained_in_the_log(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, projects = scenario
    other = commit_file(repo, "src/unrelated.ts", "export {};", T0 + timedelta(minutes=20))
    serve(monkeypatch)

    run_capture(repo, other, projects)

    log = (repo / ".rastro" / "capture.log").read_text(encoding="utf-8")
    assert f"{other[:8]}: session sess-42 captured nothing" in log
    assert "window" in log
    assert "none edited the commit's files" in log or "no assistant activity" in log


def test_missing_session_file_is_logged(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, sha, _ = scenario
    serve(monkeypatch)

    run_capture(repo, sha, tmp_path / "empty-projects")

    assert "no Claude Code session file" in (repo / ".rastro" / "capture.log").read_text(
        encoding="utf-8"
    )
