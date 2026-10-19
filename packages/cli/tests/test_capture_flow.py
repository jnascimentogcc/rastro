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


# ---------------------------------------------------------------- capture window (squash merges)

NOON = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def configured(repo: Path) -> Path:
    save_config(repo, Config(api_url="http://rastro.test"))
    save_token(repo, "rst_token")
    return repo


def session_editing(repo: Path, projects: Path, name: str, start: datetime, rel: str) -> None:
    builder = SessionBuilder(str(repo), session_id=name, start=start)
    builder.human(f"Change {rel} please ({name})")
    builder.assistant_text(f"Editing {rel}")
    builder.tool("Edit", {"file_path": str(repo / rel)}, tool_id=f"t-{name}")
    builder.write(projects / "encoded" / f"{name}.jsonl")


def squash_scenario(repo: Path, projects: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Conversation about B, then GitHub's squash of PR A lands, then B is committed."""
    a_commit = commit_file(repo, "src/a.ts", "a", NOON - timedelta(minutes=10))
    run_capture(repo, a_commit, projects)  # commit A of the open PR, captured at the time
    session_editing(repo, projects, "sess-b", NOON - timedelta(minutes=4), "src/b.ts")
    commit_file(repo, "squash.txt", "s", NOON + timedelta(seconds=34), "Squash PR A")
    return commit_file(repo, "src/b.ts", "b", NOON + timedelta(minutes=6, seconds=34))


def test_conversation_before_a_squash_parent_is_captured(
    configured: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "claude" / "projects"
    seen = serve(monkeypatch)
    sha = squash_scenario(configured, projects, monkeypatch)

    run_capture(configured, sha, projects)

    assert [json.loads(r.content)["session_ref"] for r in seen] == ["sess-b"]


def test_squash_without_capture_history_is_explained_and_recoverable(
    configured: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "claude" / "projects"
    seen = serve(monkeypatch)
    commit_file(configured, "src/a.ts", "a", NOON - timedelta(minutes=10))
    session_editing(configured, projects, "sess-b", NOON - timedelta(minutes=4), "src/b.ts")
    commit_file(configured, "squash.txt", "s", NOON + timedelta(seconds=34), "Squash PR A")
    sha = commit_file(configured, "src/b.ts", "b", NOON + timedelta(minutes=6, seconds=34))

    run_capture(configured, sha, projects)  # no history: the automatic window misses it

    assert seen == []
    log = (configured / ".rastro" / "capture.log").read_text(encoding="utf-8")
    assert f"rastro capture --sha {sha} --since 2026-10-04T12:06:34+00:00" in log
    assert "a wider window would capture" in log

    monkeypatch.chdir(configured)
    monkeypatch.setenv("RASTRO_CLAUDE_PROJECTS_DIR", str(projects))
    result = runner.invoke(app, ["capture", "--sha", sha, "--since", "2026-10-05T11:00:00+00:00"])

    assert result.exit_code == 0, result.output
    assert "1 session(s) captured" in result.output
    assert [json.loads(r.content)["commit_sha"] for r in seen] == [sha]


def test_since_recovery_without_matching_edits_exits_nonzero(
    configured: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch)
    sha = commit_file(configured, "src/b.ts", "b", NOON)
    monkeypatch.chdir(configured)
    monkeypatch.setenv("RASTRO_CLAUDE_PROJECTS_DIR", str(tmp_path / "none"))

    result = runner.invoke(app, ["capture", "--sha", sha, "--since", "2026-10-05T10:00"])

    assert result.exit_code == 3
    assert "0 session(s)" in result.output


def test_consecutive_commits_do_not_share_edits(
    configured: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "claude" / "projects"
    seen = serve(monkeypatch)
    builder = SessionBuilder(str(configured), session_id="sess-1", start=NOON - timedelta(hours=1))
    builder.human("first task on shared.ts")
    builder.tool("Edit", {"file_path": str(configured / "shared.ts")}, tool_id="t1")
    builder.write(projects / "e" / "sess-1.jsonl")
    first = commit_file(configured, "shared.ts", "1", NOON - timedelta(minutes=50))
    run_capture(configured, first, projects)

    later = SessionBuilder(str(configured), session_id="sess-2", start=NOON - timedelta(minutes=30))
    later.human("second task on shared.ts")
    later.tool("Edit", {"file_path": str(configured / "shared.ts")}, tool_id="t2")
    later.write(projects / "e" / "sess-2.jsonl")
    second = commit_file(configured, "shared.ts", "2", NOON - timedelta(minutes=20))
    run_capture(configured, second, projects)

    by_commit = {json.loads(r.content)["commit_sha"] for r in seen}
    assert by_commit == {first, second}
    second_bodies = [
        json.loads(r.content) for r in seen if json.loads(r.content)["commit_sha"] == second
    ]
    assert [b["session_ref"] for b in second_bodies] == ["sess-2"]  # sess-1 ended before `first`
    assert "first task" not in json.dumps(second_bodies)


def test_first_commit_of_a_repository_uses_the_24_hour_fallback(
    configured: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "claude" / "projects"
    seen = serve(monkeypatch)
    session_editing(configured, projects, "sess-0", NOON - timedelta(hours=3), "src/a.ts")
    sha = commit_file(configured, "src/a.ts", "a", NOON)

    run_capture(configured, sha, projects)

    assert [json.loads(r.content)["session_ref"] for r in seen] == ["sess-0"]
    assert sha in (configured / ".rastro" / "captured.json").read_text(encoding="utf-8")
