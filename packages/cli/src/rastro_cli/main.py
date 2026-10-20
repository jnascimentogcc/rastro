import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer

from rastro_cli import agent_install, capture, client, git, render, rules_cache
from rastro_cli.config import (
    DEFAULT_API_URL,
    Config,
    load_config,
    load_token,
    save_config,
    save_token,
)
from rastro_cli.hook import HookError, install_post_commit

app = typer.Typer(help="Rastro: keep the why behind AI-generated code.", no_args_is_help=True)
agent_app = typer.Typer(
    help="Hooks that tell your coding agent the recorded rules before it edits a file.",
    no_args_is_help=True,
)
app.add_typer(agent_app, name="agent")

# The hook must never fail a commit, so the hook-facing commands always exit 0.


@app.command()
def init(
    api_url: Annotated[
        str, typer.Option(envvar="RASTRO_API_URL", help="Rastro server URL.")
    ] = DEFAULT_API_URL,
    token: Annotated[
        str | None,
        typer.Option(envvar="RASTRO_TOKEN", help="Repository token (prompted if omitted)."),
    ] = None,
    skip_verify: Annotated[
        bool, typer.Option(help="Do not check the token with the server.")
    ] = False,
) -> None:
    """Authenticate this repository and install the post-commit hook."""
    try:
        root = git.repo_root(Path.cwd())
    except git.GitError:
        typer.echo("Not inside a git repository.", err=True)
        raise typer.Exit(1) from None

    api_url = api_url.rstrip("/")
    token = token or typer.prompt("Repository token", hide_input=True)

    repository_id: str | None = None
    if not skip_verify:
        try:
            identity = _verify(token, api_url)
        except httpx.HTTPStatusError as error:
            typer.echo(
                f"The server rejected the token (HTTP {error.response.status_code}).", err=True
            )
            raise typer.Exit(1) from None
        except httpx.HTTPError as error:
            typer.echo(f"Could not reach {api_url}: {error}", err=True)
            raise typer.Exit(1) from None
        repository = identity["full_name"]
        repository_id = identity.get("repository_id")
        typer.echo(f"Authenticated for {repository}.")

    if not save_token(root, token):
        typer.echo(
            "No system keychain available: set RASTRO_TOKEN in your environment instead.",
            err=True,
        )
    save_config(root, Config(api_url=api_url, repository_id=repository_id))
    _exclude_data_dir(root)

    try:
        result = install_post_commit(git.hooks_dir(root))
    except HookError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"post-commit hook: {result}.")

    if shutil.which("rastro") is None:
        typer.echo(
            "Warning: `rastro` is not on your PATH, so the hook cannot find it. "
            "Install it with `pipx install rastro-cli` or `uv tool install rastro-cli`.",
            err=True,
        )


def _verify(token: str, api_url: str) -> dict[str, str]:
    return {k: str(v) for k, v in client.whoami(token, api_url).items()}


def _exclude_data_dir(root: Path) -> None:
    """Keep .rastro/ (pending captures with conversation excerpts) out of commits."""
    _exclude_from_git(root, ".rastro/")


def _exclude_from_git(root: Path, pattern: str) -> None:
    exclude = git.info_exclude_path(root)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern not in current.splitlines():
        separator = "" if current.endswith("\n") or not current else "\n"
        exclude.write_text(current + separator + pattern + "\n", encoding="utf-8")


@app.command("capture")
def capture_hook(
    sha: Annotated[
        str | None, typer.Option(help="Commit to recapture (default: HEAD, in the background).")
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            help="Recovery: start the session window at this ISO time (e.g. 2026-10-01T11:30) "
            "instead of the automatic one, capture and upload now. A time without an offset is "
            "local time."
        ),
    ] = None,
) -> None:
    """Hook entry point: return immediately and capture in the background.

    `rastro capture --sha <sha> --since <time>` recaptures a commit whose conversation fell
    outside the automatic window (see `.rastro/capture.log`); the upload is idempotent.
    """
    if since is not None:
        _recapture(sha, since)
        return
    try:
        root = git.repo_root(Path.cwd())
        if load_config(root) is None:
            return
        target = git.resolve_sha(root, sha) if sha else git.head_sha(root)
        capture.spawn_background_capture(root, target)
    except Exception:  # never break the commit
        return


def _recapture(sha: str | None, since: str) -> None:
    try:
        root = git.repo_root(Path.cwd())
        full_sha = git.resolve_sha(root, sha or "HEAD")
        start = datetime.fromisoformat(since)
    except (git.GitError, ValueError) as error:
        typer.echo(f"Cannot recapture: {error}", err=True)
        raise typer.Exit(1) from None
    if start.tzinfo is None:
        start = start.astimezone()  # naive input means local time
    if load_config(root) is None or load_token(root) is None:
        typer.echo("Not configured: run `rastro init` first.", err=True)
        raise typer.Exit(1)
    projects = os.environ.get("RASTRO_CLAUDE_PROJECTS_DIR")
    payloads = capture.collect_payloads(root, full_sha, Path(projects) if projects else None, start)
    capture.write_pending(root, payloads)
    sent, remaining = capture.flush_pending(root)
    since_text = start.isoformat(timespec="seconds")
    typer.echo(
        f"{full_sha[:8]}: {len(payloads)} session(s) captured since {since_text}; "
        f"sent {sent}, {remaining} still pending."
    )
    if not payloads:
        raise typer.Exit(3)


@app.command("capture-run", hidden=True)
def capture_run(sha: Annotated[str, typer.Option()]) -> None:
    """Background worker started by `capture`."""
    try:
        root = git.repo_root(Path.cwd())
    except git.GitError:
        return
    projects = os.environ.get("RASTRO_CLAUDE_PROJECTS_DIR")
    capture.run_capture(root, sha, Path(projects) if projects else None)


@app.command()
def flush() -> None:
    """Resend captures that could not be uploaded earlier."""
    try:
        root = git.repo_root(Path.cwd())
    except git.GitError:
        typer.echo("Not inside a git repository.", err=True)
        raise typer.Exit(1) from None
    if load_config(root) is None or load_token(root) is None:
        typer.echo("Not configured: run `rastro init` first.", err=True)
        raise typer.Exit(1)
    sent, remaining = capture.flush_pending(root)
    typer.echo(f"Sent {sent} capture(s); {remaining} still pending.")
    if remaining:
        raise typer.Exit(2)


# --------------------------------------------------------------------------- querying rules

_STATUSES = ("active", "orphaned", "superseded", "rejected", "all")


def _api_context() -> tuple[Path, Config, str, str]:
    """(repository root, config, token, repository id), or exit with a helpful message."""
    try:
        root = git.repo_root(Path.cwd())
    except git.GitError:
        typer.echo("Not inside a git repository.", err=True)
        raise typer.Exit(1) from None
    config, token = load_config(root), load_token(root)
    if config is None or token is None:
        typer.echo("Not configured: run `rastro init` first.", err=True)
        raise typer.Exit(1)
    repository_id = config.repository_id
    if repository_id is None:  # configured before the id was cached
        identity = _call(lambda: client.whoami(token, config.api_url))
        repository_id = str(identity["repository_id"])
        save_config(root, Config(api_url=config.api_url, repository_id=repository_id))
    return root, config, token, repository_id


def _detail(response: httpx.Response) -> str:
    """The server's own explanation (a string, or the first validation error)."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(detail, list) and detail:
        detail = detail[0].get("msg", detail[0]) if isinstance(detail[0], dict) else detail[0]
    return str(detail) if detail else f"HTTP {response.status_code}"


def _call[T](request: "Callable[[], T]") -> T:
    try:
        return request()
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status in (401, 403):
            typer.echo("The server rejected the token: run `rastro init` again.", err=True)
        elif status == 404:
            typer.echo("Not found on the server.", err=True)
        elif status in (409, 422):
            typer.echo(f"The server refused it: {_detail(error.response)}", err=True)
        else:
            typer.echo(f"The server answered HTTP {status}.", err=True)
        raise typer.Exit(1) from None
    except httpx.HTTPError as error:
        typer.echo(f"Could not reach the server: {error}", err=True)
        raise typer.Exit(1) from None


def _repo_relative(value: str, root: Path) -> str | None:
    """A path typed relative to the current directory, as a repo-relative `/` path."""
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    try:
        relative = absolute.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        typer.echo(f"{value} is outside this repository.", err=True)
        raise typer.Exit(1) from None
    return None if relative == "." else relative


def _sync_rules(root: Path, config: Config, token: str, repository_id: str) -> str:
    """Refresh `.rastro/cache/rules.json`; returns the sentence to show."""
    cached = rules_cache.load(root)
    result = _call(
        lambda: client.get_rules_snapshot(
            token, config.api_url, repository_id, rules_cache.etag_of(cached)
        )
    )
    if result is None:
        rules_cache.touch(root)
        return "The rules cache is up to date."
    snapshot, etag = result
    rules_cache.save(root, snapshot, etag)
    count = len(snapshot["rules"])
    cut = " (the server cut the list: some rules are missing)" if snapshot.get("truncated") else ""
    return f"Rules cache updated: {count} rule{'s' if count != 1 else ''}{cut}."


@app.command("sync")
def sync_command() -> None:
    """Refresh the local copy of the repository's rules that the agent hook reads."""
    root, config, token, repository_id = _api_context()
    typer.echo(_sync_rules(root, config, token, repository_id))


def _repo_root_or_exit() -> Path:
    try:
        return git.repo_root(Path.cwd())
    except git.GitError:
        typer.echo("Not inside a git repository.", err=True)
        raise typer.Exit(1) from None


def _hook_works(python: str) -> bool:
    """Run the hook once on an empty event: it must answer nothing and exit 0."""
    try:
        result = subprocess.run(
            [python, "-m", agent_install.HOOK_MODULE, "pre-tool"],
            input=b"{}",
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and not result.stdout


def _install_mcp(root: Path, *, shared: bool, python: str) -> None:
    try:
        result = agent_install.install_mcp(root, shared=shared, python=python)
    except agent_install.AgentInstallError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"MCP server: {result} (.mcp.json).")
    if not shared:
        _exclude_from_git(root, ".mcp.json")
        probe = subprocess.run([python, "-c", "import rastro_mcp"], capture_output=True)
        if probe.returncode != 0:
            typer.echo(
                f"Warning: {python} cannot import rastro_mcp; install it with `uv tool install "
                "rastro-cli --with rastro-mcp` (or pip install rastro-mcp).",
                err=True,
            )
    typer.echo(
        "Claude Code asks before each tool call unless you allow them: add "
        f'"{agent_install.MCP_PERMISSION}" to permissions.allow (all three tools only read).'
    )


@agent_app.command("install")
def agent_install_command(
    shared: Annotated[
        bool,
        typer.Option(
            "--shared",
            help="Write .claude/settings.json (committed, runs on every teammate's machine) "
            "instead of the local, uncommitted .claude/settings.local.json.",
        ),
    ] = False,
    mcp: Annotated[
        bool,
        typer.Option(
            "--mcp",
            help="Also add the read-only `rastro` MCP server to .mcp.json "
            "(search and detail of the rules, on request).",
        ),
    ] = False,
) -> None:
    """Install the Claude Code hooks that show the recorded rules before a file is edited."""
    root = _repo_root_or_exit()
    python = Path(sys.executable).as_posix()
    try:
        result = agent_install.install(root, shared=shared, python=python)
    except agent_install.AgentInstallError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    path = agent_install.settings_path(root, shared=shared)
    typer.echo(f"Claude Code hooks: {result} ({path.relative_to(root).as_posix()}).")
    if shared:
        typer.echo(
            "This file is shared: teammates need `rastro-hook` on their PATH "
            "(it is installed with rastro-cli); without it the hook prints an error and does nothing.",
            err=True,
        )
    elif not _hook_works(python):
        typer.echo(
            f"Warning: {python} could not run the hook module; reinstall rastro-cli.", err=True
        )

    if mcp:
        _install_mcp(root, shared=shared, python=python)

    config, token = load_config(root), load_token(root)
    if config is None or token is None:
        typer.echo("Not configured: run `rastro init` so the rules can be downloaded.", err=True)
        return
    repository_id = config.repository_id
    if repository_id is None:
        typer.echo("Run `rastro sync` once to download the rules.")
        return
    try:
        typer.echo(_sync_rules(root, config, token, repository_id))
    except typer.Exit:
        typer.echo("The hooks are installed; run `rastro sync` when the server is reachable.")


@agent_app.command("uninstall")
def agent_uninstall_command(
    shared: Annotated[
        bool, typer.Option("--shared", help="Remove it from .claude/settings.json instead.")
    ] = False,
    mcp: Annotated[
        bool, typer.Option("--mcp", help="Also remove the `rastro` MCP server from .mcp.json.")
    ] = False,
) -> None:
    """Remove the hooks `rastro agent install` added (hooks of other tools are left alone)."""
    root = _repo_root_or_exit()
    try:
        result = agent_install.uninstall(root, shared=shared)
        mcp_result = agent_install.uninstall_mcp(root) if mcp else None
    except agent_install.AgentInstallError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    path = agent_install.settings_path(root, shared=shared)
    typer.echo(f"Claude Code hooks: {result} ({path.relative_to(root).as_posix()}).")
    if mcp_result is not None:
        typer.echo(f"MCP server: {mcp_result} (.mcp.json).")


@app.command("rules")
def rules_command(
    file: Annotated[
        str | None, typer.Option("--file", "-f", help="Only rules anchored in this file or folder.")
    ] = None,
    search: Annotated[
        str | None, typer.Option("--search", "-s", help="Search the rules (semantic if available).")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", help="active, orphaned, superseded, rejected or all.")
    ] = "active",
    unvalidated: Annotated[
        bool, typer.Option("--unvalidated", help="Only rules no human has confirmed yet.")
    ] = False,
    rejected: Annotated[
        bool, typer.Option("--rejected", help="Only rules a reviewer rejected.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print the server response.")] = False,
) -> None:
    """List the business rules of this repository and why they exist."""
    if rejected:
        status = "rejected"
    if status not in _STATUSES:
        typer.echo(f"--status must be one of: {', '.join(_STATUSES)}.", err=True)
        raise typer.Exit(2)
    root, config, token, repository_id = _api_context()
    wanted = _repo_relative(file, root) if file else None
    payload = _call(
        lambda: client.list_rules(
            token,
            config.api_url,
            repository_id,
            file=wanted,
            search=search,
            status=status,
            validated=False if unvalidated else None,
            limit=limit,
        )
    )
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(render.render_rules(payload, status=status, search=search), nl=False)


@app.command("rule")
def rule_command(
    rule_id: Annotated[str, typer.Argument(help="Rule id or a unique prefix of it.")],
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Say the rule is right as written.")
    ] = False,
    reject: Annotated[
        bool,
        typer.Option("--reject", help="Say this is not a rule; it will not be proposed again."),
    ] = False,
    correct: Annotated[
        str | None, typer.Option("--correct", help="Replace the statement with this text.")
    ] = None,
    why: Annotated[
        str | None, typer.Option("--why", help="Write the reason the rule exists.")
    ] = None,
    note: Annotated[
        str | None, typer.Option("--note", help="A comment kept with the review.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the server response.")] = False,
) -> None:
    """Show one rule, or review it with --confirm, --reject, --correct and/or --why."""
    actions = [confirm, reject, correct is not None or why is not None]
    if sum(actions) > 1:
        typer.echo("Use one of --confirm, --reject, or --correct/--why.", err=True)
        raise typer.Exit(2)
    if note and sum(actions) == 0:
        typer.echo("--note goes with --confirm, --reject, --correct or --why.", err=True)
        raise typer.Exit(2)
    root, config, token, repository_id = _api_context()
    full_id = _resolve_rule_id(rule_id, config, token, repository_id)

    if sum(actions) == 0:
        detail = _call(lambda: client.get_rule(token, config.api_url, repository_id, full_id))
        typer.echo(
            json.dumps(detail, indent=2) if as_json else render.render_rule(detail), nl=False
        )
        return

    reviewer = _reviewer(root)
    body: dict[str, str | None] = {"reviewer": reviewer, "note": note}
    if confirm:
        body["verdict"] = "confirmed"
    elif reject:
        body["verdict"] = "rejected"
    elif correct is not None:
        body.update(verdict="corrected", statement=correct, rationale=why)
    else:
        body.update(verdict="rationale_added", rationale=why)
    result = _call(lambda: client.review_rule(token, config.api_url, repository_id, full_id, body))
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(
        f"Recorded: {body['verdict']} by {reviewer} (not verified; the CLI trusts git config)."
    )
    typer.echo(render.render_rule(result), nl=False)


def _resolve_rule_id(rule_id: str, config: Config, token: str, repository_id: str) -> str:
    if len(rule_id) == 36:
        return rule_id
    listing = _call(
        lambda: client.list_rules(token, config.api_url, repository_id, status="all", limit=100)
    )
    matches = [r["id"] for r in listing["items"] if r["id"].startswith(rule_id.lower())]
    if not matches:
        typer.echo(f"No rule has an id starting with {rule_id}.", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"{rule_id} is ambiguous: {', '.join(m[:8] for m in matches)}.", err=True)
        raise typer.Exit(1)
    return str(matches[0])


def _reviewer(root: Path) -> str:
    """Who is reviewing: the git email. The server cannot verify it, and says so."""
    try:
        email = git.run_git(["config", "user.email"], root).strip()
    except git.GitError:
        email = ""
    if not email:
        typer.echo("Set your identity first: git config user.email you@example.com", err=True)
        raise typer.Exit(1)
    return email
