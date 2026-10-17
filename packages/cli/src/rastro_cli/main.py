import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import httpx
import typer

from rastro_cli import capture, client, git, render
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
    exclude = git.info_exclude_path(root)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".rastro/" not in current.splitlines():
        separator = "" if current.endswith("\n") or not current else "\n"
        exclude.write_text(current + separator + ".rastro/\n", encoding="utf-8")


@app.command("capture")
def capture_hook() -> None:
    """Hook entry point: return immediately and capture in the background."""
    try:
        root = git.repo_root(Path.cwd())
        if load_config(root) is None:
            return
        capture.spawn_background_capture(root, git.head_sha(root))
    except Exception:  # never break the commit
        return


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

_STATUSES = ("active", "orphaned", "superseded", "all")


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


def _call[T](request: "Callable[[], T]") -> T:
    try:
        return request()
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status in (401, 403):
            typer.echo("The server rejected the token: run `rastro init` again.", err=True)
        elif status == 404:
            typer.echo("Not found on the server.", err=True)
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


@app.command("rules")
def rules_command(
    file: Annotated[
        str | None, typer.Option("--file", "-f", help="Only rules anchored in this file or folder.")
    ] = None,
    search: Annotated[
        str | None, typer.Option("--search", "-s", help="Search the rules (semantic if available).")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", help="active, orphaned, superseded or all.")
    ] = "active",
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print the server response.")] = False,
) -> None:
    """List the business rules of this repository and why they exist."""
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
    as_json: Annotated[bool, typer.Option("--json", help="Print the server response.")] = False,
) -> None:
    """Show one rule with its code anchors and the history behind it."""
    _, config, token, repository_id = _api_context()
    full_id = rule_id
    if len(rule_id) != 36:
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
        full_id = matches[0]
    detail = _call(lambda: client.get_rule(token, config.api_url, repository_id, full_id))
    typer.echo(json.dumps(detail, indent=2) if as_json else render.render_rule(detail), nl=False)
