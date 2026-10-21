import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from rastro_cli import hook_entry, rules_cache

RULES = [
    {"id": "r1", "statement": "A", "validation": "validated", "anchors": []},
    {"id": "r2", "statement": "B", "validation": "unvalidated", "anchors": []},
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".rastro").mkdir()
    return tmp_path


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(hook_entry, "_spawn_sync", calls.append)
    return calls


def start(root: Path, source: str = "startup", session: str = "s1") -> dict[str, Any] | None:
    event = {
        "session_id": session,
        "cwd": str(root),
        "hook_event_name": "SessionStart",
        "source": source,
    }
    return hook_entry.session_start(json.dumps(event).encode())


def configure(root: Path) -> None:
    (root / ".rastro" / "config.json").write_text('{"api_url": "http://x"}', encoding="utf-8")


def age_cache(root: Path, seconds: int) -> None:
    path = rules_cache.rules_path(root)
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_it_tells_the_model_how_many_rules_exist_and_how_many_a_person_confirmed(
    root: Path, spawned: list[str]
) -> None:
    rules_cache.save(root, {"head_sha": "abc", "rules": RULES}, '"e"')

    answer = start(root)

    assert answer is not None
    out = answer["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "2 recorded business rule(s), 1 confirmed by a person" in out["additionalContext"]
    assert "not instructions" in out["additionalContext"]
    assert "ask the user to confirm before you edit" in out["additionalContext"]


def test_without_rules_the_model_hears_nothing(root: Path, spawned: list[str]) -> None:
    assert start(root) is None
    rules_cache.save(root, {"rules": []}, '"e"')
    assert start(root) is None


def test_a_missing_or_stale_cache_starts_a_background_sync_in_a_configured_repository(
    root: Path, spawned: list[str]
) -> None:
    start(root)
    assert spawned == []  # not configured: nothing to sync against

    configure(root)
    start(root)
    assert spawned == [str(root)]  # configured, no cache yet

    rules_cache.save(root, {"rules": RULES}, '"e"')
    spawned.clear()
    start(root)
    assert spawned == []  # fresh

    age_cache(root, hook_entry.STALE_AFTER_SECONDS + 5)
    start(root)
    assert spawned == [str(root)]  # stale


def test_the_session_sees_the_cache_that_existed_when_it_started(
    root: Path, spawned: list[str]
) -> None:
    configure(root)
    assert start(root) is None  # first ever session: the sync is only starting


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_losing_the_context_makes_every_file_new_again_for_that_session(
    root: Path, spawned: list[str], source: str
) -> None:
    directory = Path(rules_cache.cache_dir(root))
    directory.mkdir(parents=True)
    mine = [directory / "informed-s1-main.json", directory / "informed-s1-aa7f.json"]
    others = [directory / "informed-s2-main.json", directory / "rules.json"]
    for path in [*mine, *others]:
        path.write_text("[]", encoding="utf-8")

    start(root, source=source)

    assert [p.exists() for p in mine] == [False, False]
    assert [p.exists() for p in others] == [True, True]


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_starting_or_resuming_keeps_what_was_shown(
    root: Path, spawned: list[str], source: str
) -> None:
    directory = Path(rules_cache.cache_dir(root))
    directory.mkdir(parents=True)
    kept = directory / "informed-s1-main.json"
    kept.write_text("[]", encoding="utf-8")

    start(root, source=source)

    assert kept.exists()


def test_session_start_is_silent_outside_a_rastro_repository_and_on_garbage(
    tmp_path: Path, spawned: list[str]
) -> None:
    assert start(tmp_path) is None
    assert hook_entry.session_start(b"\xff not json") is None
    assert hook_entry.session_start(b"[]") is None


def test_the_consultation_log_has_ids_and_files_but_never_rule_text(root: Path) -> None:
    rules = [
        {
            "id": "r1",
            "statement": "SECRET BUSINESS TEXT",
            "rationale": "SECRET WHY",
            "validation": "validated",
            "anchors": [{"file_path": "src/a.ts", "qualified_name": "f", "role": "primary"}],
        }
    ]
    rules_cache.save(root, {"head_sha": "abc", "rules": rules}, '"e"')

    def event(tool: str, agent: str | None = None) -> bytes:
        body = {
            "session_id": "s1",
            "cwd": str(root),
            "tool_name": tool,
            "tool_input": {"file_path": str(root / "src" / "a.ts")},
        }
        if agent:
            body["agent_id"] = agent
        return json.dumps(body).encode()

    assert hook_entry.pre_tool(event("Read")) is not None
    assert hook_entry.pre_tool(event("Read")) is None  # nothing new was said, nothing logged
    assert hook_entry.pre_tool(event("Edit", agent="sub1")) is not None

    raw = (root / ".rastro" / "consulted.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in raw.splitlines()]
    assert [(r["via"], r["agent"], r["file"], r["rule_ids"]) for r in lines] == [
        ("read", None, "src/a.ts", ["r1"]),
        ("deny", "sub1", "src/a.ts", ["r1"]),
    ]
    assert lines[0]["session"] == "s1" and lines[0]["at"].endswith("Z")
    assert "SECRET" not in raw


def test_a_full_consultation_log_stops_growing(root: Path) -> None:
    rules_cache.save(
        root,
        {
            "rules": [
                {
                    "id": "r1",
                    "anchors": [{"file_path": "src/a.ts", "qualified_name": "f"}],
                    "statement": "x",
                }
            ]
        },
        '"e"',
    )
    log = root / ".rastro" / "consulted.jsonl"
    log.write_text("x" * (hook_entry.MAX_CONSULT_LOG_BYTES + 1), encoding="utf-8")
    event = json.dumps(
        {
            "session_id": "s",
            "cwd": str(root),
            "tool_name": "Read",
            "tool_input": {"file_path": str(root / "src" / "a.ts")},
        }
    ).encode()

    assert hook_entry.pre_tool(event) is not None  # still answers

    assert log.stat().st_size == hook_entry.MAX_CONSULT_LOG_BYTES + 1


def test_session_start_runs_as_a_process_and_exits_zero(root: Path) -> None:
    rules_cache.save(root, {"rules": RULES}, '"e"')
    event = json.dumps({"session_id": "s", "cwd": str(root), "source": "startup"}).encode()

    result = subprocess.run(
        [sys.executable, "-m", "rastro_cli.hook_entry", "session-start"],
        input=event,
        capture_output=True,
    )

    assert result.returncode == 0
    assert (
        "recorded business rule(s)"
        in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    )


def log_lines(root: Path) -> list[dict[str, Any]]:
    text = (root / ".rastro" / "consulted.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_session_start_leaves_proof_that_the_hook_ran_even_with_no_rules(
    root: Path, spawned: list[str]
) -> None:
    assert start(root, source="startup", session="s9") is None  # nothing to tell the model

    [marker] = log_lines(root)
    assert (marker["via"], marker["session"], marker["file"], marker["rule_ids"]) == (
        "start",
        "s9",
        "",
        [],
    )
    assert marker["at"].endswith("Z")


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_every_way_a_session_can_start_leaves_one_marker(
    root: Path, spawned: list[str], source: str
) -> None:
    start(root, source=source)

    assert [entry["via"] for entry in log_lines(root)] == ["start"]


def test_no_marker_outside_a_rastro_repository(tmp_path: Path, spawned: list[str]) -> None:
    start(tmp_path)

    assert not (tmp_path / ".rastro").exists()
