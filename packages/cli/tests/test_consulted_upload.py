import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cli_support import commit_file

from rastro_cli import agent_install, client, hook_entry, rules_cache
from rastro_cli.capture import (
    attach_consulted,
    consulted_path,
    discard_consulted,
    run_capture,
)
from rastro_cli.config import Config, pending_dir, save_config, save_token
from rastro_session_parsers.testing import SessionBuilder

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
NOW = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def scenario(repo: Path, tmp_path: Path) -> tuple[Path, str, Path]:
    commit_file(repo, "README.md", "hello", T0, "initial")
    builder = SessionBuilder(str(repo), session_id="sess-42", start=T0 + timedelta(minutes=1))
    builder.human("Implants cannot be booked on Friday afternoon")
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


def entry(session: str, file: str = "src/agenda.ts", at: str = NOW, **extra: object) -> str:
    return json.dumps(
        {
            "at": at,
            "session": session,
            "agent": None,
            "file": file,
            "rule_ids": ["r1", "r2"],
            "via": "read",
            **extra,
        }
    )


def write_log(root: Path, *lines: str) -> None:
    path = consulted_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def remaining(root: Path) -> list[str]:
    path = consulted_path(root)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_the_announcements_of_a_session_travel_with_its_capture(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    other = entry("another-session", file="src/other.ts")
    write_log(
        repo,
        entry("sess-42"),
        other,
        entry("sess-42", file="src/calendar.ts", agent="sub1", via="deny"),
    )
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    body = json.loads(seen[0].content)
    assert body["consulted"] == [
        {
            "at": NOW,
            "agent": None,
            "file": "src/agenda.ts",
            "rule_ids": ["r1", "r2"],
            "via": "read",
        },
        {
            "at": NOW,
            "agent": "sub1",
            "file": "src/calendar.ts",
            "rule_ids": ["r1", "r2"],
            "via": "deny",
        },
    ]
    assert "session" not in json.dumps(body["consulted"])  # it is already the payload's session_ref
    assert remaining(repo) == [other]  # the other session's line stays for its own capture


def test_a_capture_without_announcements_sends_an_empty_list(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    assert json.loads(seen[0].content)["consulted"] == []
    assert not consulted_path(repo).exists()


def test_a_failed_upload_keeps_the_announcements_in_the_pending_file_not_in_the_log(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    write_log(repo, entry("sess-42"))
    serve(monkeypatch, status=503)

    run_capture(repo, sha, projects)

    [pending] = pending_dir(repo).glob("*.json")
    assert len(json.loads(pending.read_text(encoding="utf-8"))["consulted"]) == 1
    assert remaining(repo) == []  # they will be retried inside the pending file, once


def test_announcements_older_than_a_week_and_damaged_lines_are_dropped(repo: Path) -> None:
    old = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = entry("someone")
    write_log(
        repo, entry("sess-x", at=old), "not json at all", json.dumps({"no": "session"}), fresh
    )

    discard_consulted(repo, [])

    assert remaining(repo) == [fresh]


def test_lines_the_hook_appends_while_a_capture_runs_are_not_lost(repo: Path) -> None:
    sent = entry("sess-42")
    write_log(repo, sent)
    payloads = [{"session_ref": "sess-42"}]
    used = attach_consulted(repo, payloads)
    late = entry("sess-42", file="src/late.ts")
    with consulted_path(repo).open("a", encoding="utf-8") as stream:
        stream.write(late + "\n")  # the hook announces something after the log was read

    discard_consulted(repo, used)

    assert remaining(repo) == [late]
    assert len(payloads[0]["consulted"]) == 1


def test_the_number_sent_per_capture_is_capped_keeping_the_newest(repo: Path) -> None:
    write_log(repo, *[entry("sess-42", file=f"src/f{i}.ts") for i in range(2105)])
    payloads: list[dict[str, object]] = [{"session_ref": "sess-42"}]

    attach_consulted(repo, payloads)

    sent = payloads[0]["consulted"]
    assert isinstance(sent, list) and len(sent) == 2000
    assert sent[-1]["file"] == "src/f2104.ts"


def test_from_the_hook_to_the_upload_no_rule_text_travels(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    rules = [
        {
            "id": "r-secret",
            "statement": "SECRET BUSINESS STATEMENT",
            "rationale": "SECRET REASON",
            "validation": "validated",
            "anchors": [{"file_path": "src/agenda.ts", "qualified_name": "f", "role": "primary"}],
        }
    ]
    rules_cache.save(repo, {"head_sha": "abc", "rules": rules}, '"e"')
    event = {
        "session_id": "sess-42",
        "cwd": str(repo),
        "tool_name": "Read",
        "tool_input": {"file_path": str(repo / "src" / "agenda.ts")},
    }
    assert hook_entry.pre_tool(json.dumps(event).encode()) is not None
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    body = json.loads(seen[0].content)
    assert body["consulted"][0]["rule_ids"] == ["r-secret"]
    assert body["consulted"][0]["via"] == "read"
    assert "SECRET BUSINESS STATEMENT" not in json.dumps(body)
    assert "SECRET REASON" not in json.dumps(body)


def start_line(session: str, at: str = NOW) -> str:
    return entry(session, file="", via="start", rule_ids=[], at=at)


def test_a_start_marker_proves_the_hook_ran_and_travels_with_the_capture(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    write_log(repo, start_line("sess-42"), entry("sess-42"))
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    body = json.loads(seen[0].content)
    assert body["agent_hook"] is True
    assert [item["via"] for item in body["consulted"]] == ["start", "read"]
    assert remaining(repo) == []


def test_an_installed_hook_that_left_no_marker_is_reported_as_not_having_run(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    agent_install.install(repo, shared=False, python="python")
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    assert json.loads(seen[0].content)["agent_hook"] is False


def test_without_a_hook_the_field_is_left_out_because_nobody_can_tell(
    scenario: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, projects = scenario
    seen = serve(monkeypatch)

    run_capture(repo, sha, projects)

    assert "agent_hook" not in json.loads(seen[0].content)


def test_hook_installed_looks_at_the_local_and_the_shared_settings(tmp_path: Path) -> None:
    assert agent_install.hook_installed(tmp_path) is False
    agent_install.install(tmp_path, shared=True, python="python")
    assert agent_install.hook_installed(tmp_path) is True
    agent_install.uninstall(tmp_path, shared=True)
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.local.json").write_text("{broken", encoding="utf-8")
    assert agent_install.hook_installed(tmp_path) is False  # an unreadable file counts as no
