"""Claude Code session parser. Format notes and rationale: docs/spec-rastro-mvp.md section 7.2.

All knowledge about the `.jsonl` layout lives here. Unknown row types and fields are ignored,
malformed lines are skipped, and nothing from tool results, thinking blocks or attachments is
ever kept.
"""

import json
import os
import posixpath
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rastro_session_parsers.models import CapturedSession, Excerpt
from rastro_session_parsers.redaction import redact

AGENT = "claude-code"
MAX_EXCERPT_CHARS = 40_000
MAX_SINGLE_TEXT_CHARS = 8_000
MAX_NOTE_CHARS = 2_000

_EDIT_TOOLS = {"Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path"} | {
    "NotebookEdit": "notebook_path"
}
MAX_BASH_SCAN_CHARS = 20_000

_NON_INTENT_PREFIXES = (
    "<command-",
    "<local-command",
    "this session is being continued",
    "[request interrupted",
    "caveat:",
)
_DRIVE = re.compile(r"^[A-Za-z]:/")


def default_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


# --------------------------------------------------------------------------- discovery


def find_session_files(
    repo_root: Path, since: datetime, projects_dir: Path | None = None
) -> list[Path]:
    """Session files of this repository modified since `since`, newest first.

    The project folder naming is not relied upon: each candidate's first `cwd` is compared
    with the repository root.
    """
    base = projects_dir or default_projects_dir()
    if not base.is_dir():
        return []
    cutoff = since.timestamp() - 60
    candidates = [p for p in base.glob("*/*.jsonl") if p.is_file() and p.stat().st_mtime >= cutoff]
    matching = [p for p in candidates if _session_cwd_in_repo(p, repo_root)]
    return sorted(matching, key=lambda p: p.stat().st_mtime, reverse=True)


def _session_cwd_in_repo(path: Path, repo_root: Path) -> bool:
    for row in _read_rows(path):
        cwd = row.get("cwd")
        if isinstance(cwd, str) and cwd:
            return _relative_to_root(cwd, str(repo_root), cwd="") is not None
    return False


# --------------------------------------------------------------------------- paths


def _slashes(path: str) -> str:
    return path.replace("\\", "/")


def _relative_to_root(path: str, root: str, cwd: str) -> str | None:
    """Path relative to `root` (with `/`), or None when it lies outside. Case-insensitive for
    Windows-style paths; resolves `..` and relative paths against `cwd`."""
    p, r = _slashes(path), _slashes(root).rstrip("/")
    if not (p.startswith("/") or _DRIVE.match(p)):
        p = posixpath.join(_slashes(cwd), p) if cwd else p
    p = posixpath.normpath(p)
    windows = bool(_DRIVE.match(p) or _DRIVE.match(r))
    left, right = (p.lower(), r.lower()) if windows else (p, r)
    if left == right:
        return ""
    if left.startswith(right + "/"):
        return p[len(r) + 1 :]
    return None


# --------------------------------------------------------------------------- reading


def _read_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _blocks(row: dict[str, Any]) -> list[dict[str, Any]]:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _human_text(row: dict[str, Any]) -> str | None:
    """Text of a human request, or None when the row is not one (tool result, meta row,
    compaction summary, local command...)."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
        return None
    blocks = _blocks(row)
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    origin = row.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    text = "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text").strip()
    if not text:
        return None
    if not isinstance(origin, dict) and text.lower().startswith(_NON_INTENT_PREFIXES):
        return None
    if text.lower().startswith("[request interrupted"):
        return None
    return text


# --------------------------------------------------------------------------- extraction


@dataclass
class _Session:
    rows: list[dict[str, Any]] = field(default_factory=list)
    by_uuid: dict[str, dict[str, Any]] = field(default_factory=dict)
    index: dict[str, int] = field(default_factory=dict)
    tool_ok: dict[str, bool] = field(default_factory=dict)


def _load(path: Path) -> _Session:
    session = _Session()
    for row in _read_rows(path):
        kind = row.get("type")
        is_boundary = kind == "system" and row.get("subtype") == "compact_boundary"
        if kind not in ("user", "assistant") and not is_boundary:
            continue
        uuid = row.get("uuid")
        if isinstance(uuid, str):
            if uuid in session.by_uuid:
                continue  # duplicated after compaction/resume
            session.by_uuid[uuid] = row
            session.index[uuid] = len(session.rows)
        session.rows.append(row)
        if kind == "user":
            for block in _blocks(row):
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    denied = bool(row.get("toolDenialKind"))
                    session.tool_ok[block["tool_use_id"]] = not (block.get("is_error") or denied)
    return session


def _parent(row: dict[str, Any]) -> str | None:
    parent = row.get("parentUuid") or row.get("logicalParentUuid")
    return parent if isinstance(parent, str) else None


def _intent_row(session: _Session, edit_row: dict[str, Any]) -> dict[str, Any] | None:
    """Nearest human request before the edit: walk the parent chain (following compaction
    boundaries), falling back to the closest earlier human request in file order."""
    seen: set[str] = set()
    current: dict[str, Any] | None = edit_row
    while current is not None:
        uuid = current.get("uuid")
        if not isinstance(uuid, str) or uuid in seen:
            break
        seen.add(uuid)
        if _human_text(current) is not None:
            return current
        parent = _parent(current)
        current = session.by_uuid.get(parent) if parent else None
    start = session.index.get(str(edit_row.get("uuid")), len(session.rows))
    earlier: dict[str, Any]
    for earlier in reversed(session.rows[:start]):
        if _human_text(earlier) is not None:
            return earlier
    return None


# --------------------------------------------------------------------------- bash edits

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_TOKEN_NOISE = str.maketrans({c: " " for c in "'\"`(){}[],="})
_REDIRECT = re.compile(r"(?<![0-9&<>])>>?\s*([^\s;&|<>]+)")
_SED_IN_PLACE = re.compile(r"(?:^|\s)sed\s(?:[^|;&\n]*\s)?(?:-[A-Za-z]*i|--in-place)")
_PERL_IN_PLACE = re.compile(r"(?:^|\s)perl\s(?:[^|;&\n]*\s)?-[A-Za-z]*i")
_INTERPRETER = re.compile(r"\b(?:python3?|py|node|deno|bun|ruby|php)\b")
_INTERPRETER_WRITE = re.compile(
    r"write_text|write_bytes|\.write\(|writeFile|appendFile|"
    r"open\([^)]*,\s*['\"][wax]|Set-Content|Out-File"
)
_GIT_RESTORE = re.compile(r"\bgit\s+(?:checkout|restore)\b")
_PATCHING = re.compile(r"\bgit\s+apply\b|(?:^|\s)patch\s")


def _bash_paths(text: str, repo_root: str, cwd: str, changed: dict[str, str]) -> set[str]:
    """Changed files named by path-like tokens of `text` (repo-relative or absolute, `a/` and
    `b/` diff prefixes included)."""
    found: set[str] = set()
    for token in text.translate(_TOKEN_NOISE).split():
        candidates = [token]
        if token[:2] in ("a/", "b/"):
            candidates.append(token[2:])
        for candidate in candidates:
            rel = _relative_to_root(candidate.rstrip(".:"), repo_root, cwd)
            if rel and (canonical := changed.get(rel.lower())) is not None:
                found.add(canonical)
    return found


def bash_written_files(command: str, repo_root: str, cwd: str, changed: dict[str, str]) -> set[str]:
    """Changed files a Bash command probably writes. Deliberately conservative heuristic, text
    only (the command is never executed or stored):

    - `sed -i` / `perl -i` on the file, `tee [-a] file`, a redirection `> file` / `>> file`,
      `mv` (either side), `cp` (destination, the last argument), `git checkout|restore` and
      `git apply` / `patch` (the diff headers name the file) when the file is named;
    - a `python`/`node`/... command that names the file and contains a write call
      (`write_text`, `writeFileSync`, `open(..., 'w')`...).

    Reads (`cat`, `grep`, `sed -n`, `git diff`) never count. Limits: files reached through
    globs, variables, `cd` followed by a bare name, scripts that compute the path, or a patch
    file that is only named by path are missed; a `sed -i` whose script (not its target)
    mentions a changed path is a false positive."""
    command = command[:MAX_BASH_SCAN_CHARS]
    written: set[str] = set()
    for match in _REDIRECT.finditer(command):
        written |= _bash_paths(match.group(1), repo_root, cwd, changed)
    for segment in _SEGMENT_SPLIT.split(command):
        padded = f" {segment.strip()}"
        words = segment.translate(_TOKEN_NOISE).split()
        if not words:
            continue
        if (
            _SED_IN_PLACE.search(padded)
            or _PERL_IN_PLACE.search(padded)
            or words[0] in ("tee", "mv")
            or _GIT_RESTORE.search(padded)
            or _PATCHING.search(padded)
        ):
            written |= _bash_paths(segment, repo_root, cwd, changed)
        elif words[0] == "cp":
            written |= _bash_paths(words[-1], repo_root, cwd, changed)
    if _PATCHING.search(command) or (
        _INTERPRETER.search(command) and _INTERPRETER_WRITE.search(command)
    ):
        written |= _bash_paths(command, repo_root, cwd, changed)
    return written


@dataclass(frozen=True)
class _Edit:
    row: dict[str, Any]
    tool: str
    rel_path: str


def _linked_edits(
    session: _Session,
    repo_root: str,
    changed: dict[str, str],
    since: datetime,
    until: datetime,
    branch: str | None,
) -> list[_Edit]:
    edits: list[_Edit] = []
    for row in session.rows:
        if row.get("type") != "assistant":
            continue
        when = _parse_time(row.get("timestamp"))
        if when is None or not (since <= when <= until):
            continue
        row_branch = row.get("gitBranch")
        if branch and isinstance(row_branch, str) and row_branch not in ("", "HEAD", branch):
            continue
        for block in _blocks(row):
            if block.get("type") != "tool_use" or not session.tool_ok.get(str(block.get("id"))):
                continue
            cwd = str(row.get("cwd") or "")
            if block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str):
                    for written in sorted(bash_written_files(command, repo_root, cwd, changed)):
                        edits.append(_Edit(row=row, tool="Bash", rel_path=written))
                continue
            field_name = _EDIT_TOOLS.get(str(block.get("name")))
            if field_name is None:
                continue
            raw = (block.get("input") or {}).get(field_name)
            if not isinstance(raw, str):
                continue
            rel = _relative_to_root(raw, repo_root, cwd)
            canonical = changed.get(rel.lower()) if rel else None
            if canonical is not None:
                edits.append(_Edit(row=row, tool=str(block["name"]), rel_path=canonical))
    return edits


def extract_session(
    path: Path,
    repo_root: Path,
    changed_files: Iterable[str],
    since: datetime,
    until: datetime,
    *,
    branch: str | None = None,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> CapturedSession | None:
    """Excerpts of one session relevant to a commit, or None if it did not edit any of the
    commit's files inside the window."""
    session = _load(path)
    changed = {_slashes(f).lower(): _slashes(f) for f in changed_files}
    edits = _linked_edits(session, str(repo_root), changed, since, until, branch)
    if not edits:
        return None

    intents: dict[str, dict[str, Any]] = {}
    intent_files: dict[str, set[str]] = {}
    for edit in edits:
        intent = _intent_row(session, edit.row)
        if intent is not None:
            uuid = str(intent["uuid"])
            intents[uuid] = intent
            intent_files.setdefault(uuid, set()).add(edit.rel_path)

    last_edit_index = max(session.index.get(str(e.row.get("uuid")), 0) for e in edits)
    ordered: list[Excerpt] = []

    def add(group: list[Excerpt], excerpt: Excerpt) -> None:
        group.append(excerpt)

    intent_excerpts: list[Excerpt] = []
    per_intent = max(500, min(MAX_SINGLE_TEXT_CHARS, max_chars // max(1, len(intents))))
    for uuid, row in intents.items():
        text = redact(_human_text(row) or "")[:per_intent]
        add(
            intent_excerpts,
            Excerpt(
                "user", "intent", text, str(row.get("timestamp")), tuple(sorted(intent_files[uuid]))
            ),
        )

    context_excerpts = [
        Excerpt(
            "user", "context", redact(request)[:MAX_SINGLE_TEXT_CHARS], str(row.get("timestamp"))
        )
        for row in reversed(session.rows[: last_edit_index + 1])
        if str(row.get("uuid")) not in intents and (request := _human_text(row)) is not None
    ]

    edit_excerpts: list[Excerpt] = []
    seen_paths: set[str] = set()
    for edit in edits:
        if edit.rel_path not in seen_paths:
            seen_paths.add(edit.rel_path)
            edit_excerpts.append(
                Excerpt(
                    "assistant",
                    "edit",
                    f"{edit.tool} {edit.rel_path}",
                    str(edit.row.get("timestamp")),
                    (edit.rel_path,),
                )
            )

    note_excerpts = _assistant_notes(session, changed, since, until)

    budget = max_chars
    chosen: list[Excerpt] = []
    for group in (intent_excerpts, edit_excerpts, context_excerpts, note_excerpts):
        for excerpt in group:
            cost = len(excerpt.text)
            if group is intent_excerpts or cost <= budget:
                chosen.append(excerpt)
                budget -= cost
    ordered = sorted(chosen, key=lambda e: e.timestamp)

    return CapturedSession(
        session_ref=_first_str(session.rows, "sessionId") or path.stem,
        agent=AGENT,
        agent_version=_last_str(session.rows, "version"),
        git_branch=_last_str([e.row for e in edits], "gitBranch"),
        excerpts=tuple(ordered),
    )


def explain_no_edits(
    path: Path,
    since: datetime,
    until: datetime,
    *,
    branch: str | None = None,
) -> str:
    """Why `extract_session` found nothing, for the capture log: window activity only (tool
    call counts by name), no content."""
    session = _load(path)
    tools: dict[str, int] = {}
    for row in session.rows:
        when = _parse_time(row.get("timestamp"))
        if row.get("type") != "assistant" or when is None or not (since <= when <= until):
            continue
        row_branch = row.get("gitBranch")
        if branch and isinstance(row_branch, str) and row_branch not in ("", "HEAD", branch):
            continue
        for block in _blocks(row):
            if block.get("type") == "tool_use":
                name = str(block.get("name"))[:40]
                tools[name] = tools.get(name, 0) + 1
    if not tools:
        return "no assistant activity in the window (or on this branch)"
    counts = ", ".join(f"{n} x{c}" for n, c in sorted(tools.items()))
    return (
        f"tool calls in the window ({counts}) but none edited the commit's files "
        "(successful Edit/Write/MultiEdit, or Bash that writes them)"
    )


def _assistant_notes(
    session: _Session, changed: dict[str, str], since: datetime, until: datetime
) -> list[Excerpt]:
    needles = {rel.lower() for rel in changed.values()} | {
        posixpath.basename(rel).lower() for rel in changed.values()
    }
    texts: dict[str, tuple[str, list[str]]] = {}
    for row in session.rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        key = str(message_id or row.get("uuid"))
        when = str(row.get("timestamp"))
        parts = texts.setdefault(key, (when, []))[1]
        parts.extend(str(b.get("text", "")) for b in _blocks(row) if b.get("type") == "text")
    notes: list[Excerpt] = []
    for when, parts in texts.values():
        moment = _parse_time(when)
        text = "\n".join(p for p in parts if p).strip()
        if not text or moment is None or not (since <= moment <= until):
            continue
        lowered = text.lower()
        if any(needle in lowered for needle in needles):
            notes.append(
                Excerpt("assistant", "assistant_note", redact(text)[:MAX_NOTE_CHARS], when)
            )
    return list(reversed(notes))


def _first_str(rows: list[dict[str, Any]], key: str) -> str | None:
    return next((v for r in rows if isinstance(v := r.get(key), str) and v), None)


def _last_str(rows: list[dict[str, Any]], key: str) -> str | None:
    return next((v for r in reversed(rows) if isinstance(v := r.get(key), str) and v), None)


def default_window(
    commit_time: datetime, previous_commit_time: datetime | None
) -> tuple[datetime, datetime]:
    """Window of session activity that belongs to a commit: since the previous commit (or 24 h
    before when there is none), until just after the commit."""
    start = previous_commit_time or commit_time - timedelta(hours=24)
    return start, commit_time + timedelta(seconds=60)
