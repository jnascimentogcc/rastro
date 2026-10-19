"""Entry point of the Claude Code `PreToolUse` hook: `python -m rastro_cli.hook_entry pre-tool`.

Reads the event JSON from stdin and, for a file with recorded rules, answers with the rules:

- `Read`: `additionalContext` (the model gets the rules together with the file, before any edit);
- `Edit`/`Write`/`MultiEdit` on a file not yet shown in this session (or by this subagent): a
  `deny` carrying the rules, once; repeating the edit goes through.

It must never get in the agent's way: any problem means silence and exit code 0 (never 2), with
one line in `.rastro/hook.log`. Standard library only and no heavy imports (see `rules_cache`), so
it costs little more than Python's own start-up.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from rastro_cli import rules_block, rules_cache

TYPE_CHECKING = False  # `typing` is only imported for the type checker
if TYPE_CHECKING:
    from typing import Any

READ_TOOLS = {"Read"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}
MAX_STDIN_BYTES = 2 * 1024 * 1024
MAX_FILE_PEEK_BYTES = 256 * 1024  # a Read is ranked by the symbols in the file; no need for more
WINDOWS = os.name == "nt"
DENY_NOTE = (
    "This is a notice about recorded business rules, not an error. "
    "Repeat the edit to proceed once you have taken the rules into account."
)


def _log(root: str | None, message: str) -> None:
    if root is None:
        return
    try:
        path = os.path.join(root, rules_cache.DATA_DIR, "hook.log")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pre-tool: {message}\n")
    except OSError:
        pass


def _relative(path: str, root: str) -> str | None:
    """`path` relative to the repository root with `/` separators, or None when outside it."""
    try:
        absolute = os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
        relative = os.path.relpath(absolute, root)
    except ValueError:  # another drive on Windows
        return None
    if relative == "." or relative == ".." or relative.startswith(".." + os.sep):
        return None
    return relative.replace("\\", "/")


def _informed_path(root: str, event: dict[str, Any]) -> str:
    # A subagent shares the parent's session id but not its context: key by both.
    raw = f"{event.get('session_id') or 'none'}-{event.get('agent_id') or 'main'}"
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:120]
    return os.path.join(rules_cache.cache_dir(root), f"informed-{key}.json")


def _load_informed(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        return {str(item) for item in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _mark_informed(path: str, informed: set[str], relative: str) -> None:
    informed.add(relative)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(sorted(informed)))
    except OSError:
        pass


def _text_about_to_change(event: dict[str, Any], absolute: str) -> str:
    tool_input = event.get("tool_input") or {}
    pieces: list[str] = []
    if isinstance(tool_input.get("old_string"), str):
        pieces.append(tool_input["old_string"])
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, dict) and isinstance(edit.get("old_string"), str):
            pieces.append(edit["old_string"])
    if pieces:
        return "\n".join(pieces)
    if event.get("tool_name") in READ_TOOLS:
        try:
            with open(absolute, encoding="utf-8", errors="ignore") as stream:
                return stream.read(MAX_FILE_PEEK_BYTES)
        except OSError:
            return ""
    return ""


def decide(event: dict[str, Any], root: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """The hook's JSON answer for `event`, or None to stay silent."""
    tool = event.get("tool_name")
    file_path = (event.get("tool_input") or {}).get("file_path")
    if tool not in READ_TOOLS | WRITE_TOOLS or not isinstance(file_path, str) or not file_path:
        return None
    relative = _relative(file_path, root)
    if relative is None:
        return None
    absolute = file_path if os.path.isabs(file_path) else os.path.join(root, file_path)
    rules = rules_block.rules_for_file(
        snapshot["rules"], relative, _text_about_to_change(event, absolute), ignore_case=WINDOWS
    )
    if not rules:
        return None

    informed_file = _informed_path(root, event)
    informed = _load_informed(informed_file)
    if (relative.lower() if WINDOWS else relative) in {
        item.lower() if WINDOWS else item for item in informed
    }:
        return None
    block = rules_block.render_block(rules, relative, snapshot.get("head_sha"), ignore_case=WINDOWS)
    if not block:
        return None
    _mark_informed(informed_file, informed, relative)
    if tool in READ_TOOLS:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": block}}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{block}\n{DENY_NOTE}",
        }
    }


def pre_tool(stdin: bytes) -> dict[str, Any] | None:
    root: str | None = None
    try:
        event = json.loads(stdin[:MAX_STDIN_BYTES].decode("utf-8-sig"))
        if not isinstance(event, dict):
            return None
        root = rules_cache.find_root(str(event.get("cwd") or os.getcwd()))
        if root is None:
            return None  # not a Rastro repository: nothing to say, nothing to log
        snapshot = rules_cache.load(root)
        if snapshot is None:
            _log(root, "no usable rules cache (run `rastro sync`)")
            return None
        return decide(event, root, snapshot)
    except Exception as error:  # the agent's work comes first: never fail, never exit 2
        _log(root, f"{type(error).__name__}: {error}")
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["pre-tool"]:
        answer = pre_tool(sys.stdin.buffer.read(MAX_STDIN_BYTES))
        if answer is not None:
            sys.stdout.write(json.dumps(answer))  # ASCII-escaped: safe on any console code page
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
