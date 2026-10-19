"""Build capture payloads for a commit and send them. Nothing here may break a commit."""

import json
import subprocess
import sys
import traceback
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from rastro_cli import client, git
from rastro_cli.config import (
    captured_path,
    data_dir,
    load_config,
    load_token,
    pending_dir,
    rejected_dir,
)
from rastro_session_parsers import (
    CapturedSession,
    default_window,
    explain_no_edits,
    extract_session,
    find_session_files,
    wide_window_start,
)

LOG_NAME = "capture.log"


def log(root: Path, message: str) -> None:
    try:
        directory = data_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with (directory / LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def spawn_background_capture(root: Path, sha: str) -> None:
    """Start `capture-run` as a detached process so the commit returns immediately."""
    command = [sys.executable, "-m", "rastro_cli", "capture-run", "--sha", sha]
    options: dict[str, Any] = {
        "cwd": root,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)  # noqa: S603


def to_payload(sha: str, captured: CapturedSession) -> dict[str, Any]:
    return {
        "commit_sha": sha,
        "session_ref": captured.session_ref,
        "agent": captured.agent,
        "agent_version": captured.agent_version,
        "git_branch": captured.git_branch,
        "excerpts": [asdict(e) | {"files": list(e.files)} for e in captured.excerpts],
    }


MAX_CAPTURED_HISTORY = 50


def load_captured(root: Path) -> dict[str, datetime]:
    """Commits already processed by `capture-run` (sha -> committer time), from
    `.rastro/captured.json`. Missing or damaged state means no history."""
    try:
        raw = json.loads(captured_path(root).read_text(encoding="utf-8"))
        return {str(sha): datetime.fromisoformat(str(when)) for sha, when in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def record_captured(root: Path, sha: str, when: datetime) -> None:
    """Remember that a commit was processed (whether or not it produced a capture), so the next
    commit's window starts after it. Keeps only the most recent entries."""
    captured = load_captured(root)
    captured[sha] = when
    newest = sorted(captured.items(), key=lambda item: item[1])[-MAX_CAPTURED_HISTORY:]
    try:
        path = captured_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({s: t.isoformat() for s, t in newest}, indent=1) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def last_captured_time(root: Path, sha: str, commit_time: datetime) -> datetime | None:
    """Time of the most recent already-captured commit older than this one."""
    times = [t for s, t in load_captured(root).items() if s != sha and t < commit_time]
    return max(times, default=None)


def collect_payloads(
    root: Path,
    sha: str,
    projects_dir: Path | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Captured sessions for a commit. `since` forces the window start (manual recovery)."""
    files = git.changed_files(root, sha)
    if not files:
        return []
    committed = git.commit_time(root, sha)
    wide = wide_window_start(committed)
    if since is not None:
        start, end = since, committed + timedelta(seconds=60)
    else:
        start, end = default_window(
            committed,
            git.parent_commit_time(root, sha),
            last_captured_time(root, sha, committed),
        )
    branch = git.current_branch(root)
    payloads: list[dict[str, Any]] = []
    window = f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}"
    sessions = find_session_files(root, min(start, wide), projects_dir)
    if not sessions:
        log(root, f"{sha[:8]}: no Claude Code session file for this repository since {window}")
    for path in sessions:
        captured = extract_session(path, root, files, start, end, branch=branch)
        if captured is not None:
            payloads.append(to_payload(sha, captured))
            continue
        reason = explain_no_edits(path, start, end, branch=branch)
        message = f"{sha[:8]}: session {path.stem} captured nothing, window {window}: {reason}"
        if since is None and start > wide:
            wider = extract_session(path, root, files, wide, end, branch=branch)
            if wider is not None:
                message += (
                    f"; a wider window would capture {len(wider.excerpts)} excerpt(s), "
                    f"to recover run: rastro capture --sha {sha} --since "
                    f"{wide.isoformat(timespec='seconds')}"
                )
        log(root, message)
    return payloads


def write_pending(root: Path, payloads: list[dict[str, Any]]) -> list[Path]:
    directory = pending_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for payload in payloads:
        safe_ref = "".join(c if c.isalnum() or c in "-_" else "_" for c in payload["session_ref"])
        path = directory / f"{payload['commit_sha']}-{safe_ref}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        written.append(path)
    return written


def flush_pending(root: Path) -> tuple[int, int]:
    """Send every pending capture. Returns (sent, remaining).

    Sent or permanently rejected (4xx other than auth/rate limit) captures leave `pending/`;
    network errors, 5xx, 401/403 and 429 keep the file for the next attempt.
    """
    directory = pending_dir(root)
    files = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not files:
        return 0, 0
    config, token = load_config(root), load_token(root)
    if config is None or token is None:
        log(root, "not configured: run `rastro init`")
        return 0, len(files)

    sent = 0
    with client.make_client(token, config.api_url) as http:
        for path in files:
            try:
                response = http.post(
                    "/v1/sessions",
                    content=path.read_bytes(),
                    headers={"Content-Type": "application/json"},
                )
            except httpx.HTTPError as error:
                log(root, f"upload failed for {path.name}: {error}")
                break  # server unreachable: do not hammer it with the rest
            if response.is_success:
                path.unlink()
                sent += 1
            elif response.status_code in (401, 403, 429) or response.status_code >= 500:
                log(root, f"upload deferred for {path.name}: HTTP {response.status_code}")
                if response.status_code in (401, 403):
                    break
            else:
                log(root, f"upload rejected for {path.name}: HTTP {response.status_code}")
                rejected_dir(root).mkdir(parents=True, exist_ok=True)
                path.replace(rejected_dir(root) / path.name)
    return sent, len(list(directory.glob("*.json")))


def run_capture(
    root: Path,
    sha: str,
    projects_dir: Path | None = None,
    since: datetime | None = None,
) -> None:
    """Background entry point: collect, persist to pending/, then flush. Never raises.

    With `since` (manual recovery) the window start is forced and the capture history is left
    alone; otherwise the commit is recorded so the next window starts after it.
    """
    try:
        if load_config(root) is None:
            return
        payloads = collect_payloads(root, sha, projects_dir, since)
        write_pending(root, payloads)
        if since is None:
            record_captured(root, sha, git.commit_time(root, sha))
        flush_pending(root)
    except Exception:  # a capture problem must never surface in the user's workflow
        log(root, "capture failed:\n" + traceback.format_exc())
