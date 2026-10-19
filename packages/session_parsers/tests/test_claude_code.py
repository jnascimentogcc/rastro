from datetime import timedelta
from pathlib import Path

from rastro_session_parsers import (
    CapturedSession,
    default_window,
    extract_session,
    find_session_files,
)
from rastro_session_parsers.testing import T0, SessionBuilder

ROOT_WIN = "C:\\Users\\dev\\proj\\clinic"
ROOT_POSIX = "/home/dev/clinic"
SINCE = T0 - timedelta(hours=1)
UNTIL = T0 + timedelta(hours=1)


def extract(
    builder: SessionBuilder, tmp_path: Path, changed: set[str], root: str, **kw: object
) -> CapturedSession | None:
    path = builder.write(tmp_path / "proj" / "sess-1.jsonl")
    return extract_session(path, Path(root), changed, SINCE, UNTIL, **kw)  # type: ignore[arg-type]


def kinds(captured: CapturedSession) -> list[str]:
    return [e.kind for e in captured.excerpts]


def test_links_edit_to_human_request_and_ignores_tool_results(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_WIN)
    b.human("Implants cannot be booked on Friday afternoon, Dr. Mendes needs 48h of follow-up.")
    b.assistant_text("I will update src/agenda.ts")
    b.tool(
        "Edit",
        {"file_path": f"{ROOT_WIN}\\src\\agenda.ts", "old_string": "a", "new_string": "b"},
        tool_id="t1",
    )

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_WIN)

    assert captured is not None
    assert captured.session_ref == "sess-1"
    assert captured.agent == "claude-code"
    assert captured.agent_version == "2.1.288"
    assert captured.git_branch == "main"
    intent = next(e for e in captured.excerpts if e.kind == "intent")
    assert "Friday afternoon" in intent.text
    assert intent.files == ("src/agenda.ts",)
    dumped = repr(captured)
    assert "SECRET FILE CONTENT" not in dumped
    assert "old_string" not in dumped


def test_edit_marker_keeps_path_but_not_content(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("add discount")
    b.tool(
        "Write",
        {"file_path": f"{ROOT_POSIX}/src/price.ts", "content": "const key='abc'"},
        tool_id="t1",
    )

    captured = extract(b, tmp_path, {"src/price.ts"}, ROOT_POSIX)

    assert captured is not None
    edit = next(e for e in captured.excerpts if e.kind == "edit")
    assert edit.text == "Write src/price.ts"
    assert "const key" not in repr(captured)


def test_session_without_edits_to_changed_files_is_ignored(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("touch other file")
    b.tool(
        "Edit",
        {"file_path": f"{ROOT_POSIX}/src/other.ts", "old_string": "a", "new_string": "b"},
        tool_id="t1",
    )

    assert extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX) is None


def test_denied_and_failed_edits_are_discarded(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("change agenda")
    path = f"{ROOT_POSIX}/src/agenda.ts"
    b.tool("Edit", {"file_path": path}, tool_id="t1", error=True)
    b.tool("Edit", {"file_path": path}, tool_id="t2", denial="user-rejected")
    b.tool("Edit", {"file_path": path}, tool_id="t3", denial="permission-rule")

    assert extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX) is None


def test_edits_outside_repo_root_are_ignored(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_WIN)
    b.human("write a plan")
    b.tool(
        "Write",
        {"file_path": "C:\\Users\\dev\\.claude\\plans\\plan.md", "content": "x"},
        tool_id="t1",
    )
    b.tool(
        "Edit", {"file_path": "C:\\Users\\dev\\proj\\clinic-other\\src\\agenda.ts"}, tool_id="t2"
    )

    assert extract(b, tmp_path, {"src/agenda.ts", "plan.md"}, ROOT_WIN) is None


def test_windows_paths_match_case_insensitively_and_mixed_separators(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_WIN)
    b.human("fix")
    b.tool("Edit", {"file_path": "c:/users/DEV/proj/Clinic/src/Agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_WIN)

    assert captured is not None
    assert captured.excerpts[-1].files == ("src/agenda.ts",) or "edit" in kinds(captured)


def test_relative_edit_path_resolves_against_row_cwd(tmp_path: Path) -> None:
    b = SessionBuilder(f"{ROOT_POSIX}/src")
    b.human("fix")
    b.tool("Edit", {"file_path": "agenda.ts"}, tool_id="t1")

    assert extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX) is not None


def test_non_intent_user_rows_are_not_treated_as_requests(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("real request: add the discount")
    b.raw_user("This session is being continued from a previous conversation...")
    b.raw_user("<command-name>/compact</command-name>")
    b.raw_user("<local-command-stdout>Compacted</local-command-stdout>")
    b.raw_user([{"type": "text", "text": "[Image: source: /tmp/1.png]"}], isMeta=True)
    b.raw_user([{"type": "text", "text": "[Request interrupted by user]"}])
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    user_texts = [e.text for e in captured.excerpts if e.role == "user"]
    assert user_texts == ["real request: add the discount"]


def test_intent_is_found_across_compaction_boundary(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("original business request")
    b.assistant_text("working on it")
    b.compact()
    b.raw_user("This session is being continued from a previous conversation...")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    intent = next(e for e in captured.excerpts if e.kind == "intent")
    assert intent.text == "original business request"


def test_each_edit_gets_its_own_nearest_request_and_others_become_context(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("first request about agenda")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")
    b.human("second request about unrelated css")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/style.css"}, tool_id="t2")
    b.human("third request about price")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/price.ts"}, tool_id="t3")

    captured = extract(b, tmp_path, {"src/agenda.ts", "src/price.ts"}, ROOT_POSIX)

    assert captured is not None
    by_kind = {k: [e.text for e in captured.excerpts if e.kind == k] for k in ("intent", "context")}
    assert by_kind["intent"] == ["first request about agenda", "third request about price"]
    assert by_kind["context"] == ["second request about unrelated css"]


def test_assistant_message_split_across_rows_is_grouped(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("fix")
    b.assistant_text("Part one about src/agenda.ts", message_id="mX")
    b.assistant_text("part two", message_id="mX")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    notes = [e.text for e in captured.excerpts if e.kind == "assistant_note"]
    assert notes == ["Part one about src/agenda.ts\npart two"]


def test_assistant_text_unrelated_to_changed_files_is_dropped(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("fix")
    b.assistant_text("Talking about something else entirely")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    assert "assistant_note" not in kinds(captured)


def test_secrets_are_redacted_in_every_excerpt(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human(
        "use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 and postgres://u:hunter2pass@db/x"
    )
    b.assistant_text("Updating src/agenda.ts with token ghp_" + "a" * 36)
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    dumped = repr(captured)
    for leaked in ("sk-abcdef", "hunter2pass", "ghp_aaaa"):
        assert leaked not in dumped
    assert "[REDACTED]" in dumped


def test_size_limit_keeps_intents_and_drops_lower_priority(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    for i in range(20):
        b.human(f"context request number {i} " + "x" * 400)
    b.human("the intent")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX, max_chars=2000)

    assert captured is not None
    assert "intent" in kinds(captured)
    assert sum(len(e.text) for e in captured.excerpts) <= 2000 + len("the intent")
    context = [e for e in captured.excerpts if e.kind == "context"]
    assert 0 < len(context) < 20
    assert "number 19" in context[-1].text  # the closest requests survive


def test_branch_mismatch_skips_edit_but_detached_head_is_accepted(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX, branch="other-branch")
    b.human("fix")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")
    assert extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX, branch="main") is None

    detached = SessionBuilder(ROOT_POSIX, branch="HEAD")
    detached.human("fix")
    detached.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")
    assert extract(detached, tmp_path, {"src/agenda.ts"}, ROOT_POSIX, branch="main") is not None


def test_time_window_excludes_edits_outside_it(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("fix")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")
    path = b.write(tmp_path / "proj" / "s.jsonl")

    late = extract_session(
        path, Path(ROOT_POSIX), {"src/agenda.ts"}, T0 + timedelta(days=1), T0 + timedelta(days=2)
    )

    assert late is None


def test_duplicate_rows_are_deduplicated(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("only once")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1")
    b.rows = b.rows + b.rows  # as happens after compaction/resume

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    assert [e.text for e in captured.excerpts if e.kind == "intent"] == ["only once"]


def test_subagent_edit_is_attributed_to_the_main_request(tmp_path: Path) -> None:
    b = SessionBuilder(ROOT_POSIX)
    b.human("delegate this refactor")
    b.tool("Edit", {"file_path": f"{ROOT_POSIX}/src/agenda.ts"}, tool_id="t1", isSidechain=True)

    captured = extract(b, tmp_path, {"src/agenda.ts"}, ROOT_POSIX)

    assert captured is not None
    assert [e.text for e in captured.excerpts if e.kind == "intent"] == ["delegate this refactor"]


def test_find_session_files_matches_by_cwd_not_folder_name(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    mine_builder = SessionBuilder(f"{ROOT_POSIX}/sub", session_id="mine")
    mine_builder.human("hello")
    mine = mine_builder.write(projects / "any-name" / "mine.jsonl")
    other_builder = SessionBuilder("/home/dev/other", session_id="other")
    other_builder.human("hello")
    other_builder.write(projects / "x" / "other.jsonl")

    found = find_session_files(Path(ROOT_POSIX), T0 - timedelta(days=3650), projects)

    assert found == [mine]


def test_default_window() -> None:
    start, end = default_window(T0, T0 - timedelta(hours=2))
    assert (start, end) == (T0 - timedelta(hours=2), T0 + timedelta(seconds=60))
    assert default_window(T0, None)[0] == T0 - timedelta(hours=24)


def bash_session(root: str, command: str, **tool_kw: object) -> SessionBuilder:
    b = SessionBuilder(root)
    b.human("remove the cancellation rule")
    b.tool("Bash", {"command": command}, tool_id="t1", **tool_kw)  # type: ignore[arg-type]
    return b


def test_bash_sed_in_place_counts_as_an_edit(tmp_path: Path) -> None:
    command = (
        "sed -i '3,5d' src/domain/cancellation.ts && "
        "sed -i '2a const MIN = 1;' src/domain/cancellation.ts"
    )
    b = bash_session(ROOT_POSIX, command)

    captured = extract(b, tmp_path, {"src/domain/cancellation.ts"}, ROOT_POSIX)

    assert captured is not None
    edits = [e for e in captured.excerpts if e.kind == "edit"]
    assert [e.files for e in edits] == [("src/domain/cancellation.ts",)]
    assert edits[0].text == "Bash src/domain/cancellation.ts"
    assert "sed" not in "".join(e.text for e in captured.excerpts)
    assert any(e.kind == "intent" for e in captured.excerpts)


def test_bash_writers_count(tmp_path: Path) -> None:
    f = "src/a.ts"
    absolute = f"{ROOT_POSIX}/{f}"
    commands = [
        f"echo 'x' > {f}",
        f"echo x >> {absolute}",
        f"cat patch.txt | tee {f}",
        f"mv /tmp/new.ts {f}",
        f"cp /tmp/new.ts {f}",
        f"git checkout -- {f}",
        f"git apply <<'EOF'\n--- a/{f}\n+++ b/{f}\n@@ -1 +1 @@\n-a\n+b\nEOF",
        f"python - <<'EOF'\nfrom pathlib import Path\nPath('{f}').write_text('x')\nEOF",
        f"node -e \"require('fs').writeFileSync('{f}', 'x')\"",
        f"perl -pi -e 's/a/b/' {f}",
    ]
    for command in commands:
        b = bash_session(ROOT_POSIX, command)
        assert extract(b, tmp_path, {f}, ROOT_POSIX) is not None, command


def test_bash_reads_do_not_count(tmp_path: Path) -> None:
    f = "src/a.ts"
    commands = [
        f"cat {f}",
        f"grep -n cancel {f}",
        f"sed -n '1,20p' {f}",
        f"git diff {f}",
        f"cat {f} > /tmp/copy.ts",
        f"cp {f} /tmp/backup.ts",
        f"python -c \"print(open('{f}').read())\"",
        f"echo {f} 2>/dev/null",
    ]
    for command in commands:
        b = bash_session(ROOT_POSIX, command)
        assert extract(b, tmp_path, {f}, ROOT_POSIX) is None, command


def test_failed_or_denied_bash_edits_are_ignored(tmp_path: Path) -> None:
    command = "sed -i 's/a/b/' src/a.ts"
    assert (
        extract(bash_session(ROOT_POSIX, command, error=True), tmp_path, {"src/a.ts"}, ROOT_POSIX)
        is None
    )
    denied = bash_session(ROOT_POSIX, command, denial="user-rejected")
    assert extract(denied, tmp_path, {"src/a.ts"}, ROOT_POSIX) is None


def test_bash_edit_outside_repo_or_unchanged_file_is_ignored(tmp_path: Path) -> None:
    outside = bash_session(ROOT_POSIX, "sed -i 's/a/b/' /home/dev/clinic-other/src/a.ts")
    assert extract(outside, tmp_path, {"src/a.ts"}, ROOT_POSIX) is None
    other = bash_session(ROOT_POSIX, "sed -i 's/a/b/' src/b.ts")
    assert extract(other, tmp_path, {"src/a.ts"}, ROOT_POSIX) is None


def test_bash_edit_with_secret_never_leaks_command(tmp_path: Path) -> None:
    b = bash_session(ROOT_POSIX, "sed -i 's/a/sk-abcdefghijklmnopqrstuvwxyz1234/' src/a.ts")

    captured = extract(b, tmp_path, {"src/a.ts"}, ROOT_POSIX)

    assert captured is not None
    assert "sk-abcdef" not in str(captured)
