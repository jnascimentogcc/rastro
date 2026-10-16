import os
import shutil
from pathlib import Path
from typing import Annotated

import httpx
import typer

from rastro_cli import capture, git
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

    if not skip_verify:
        try:
            repository = _verify(token, api_url)
        except httpx.HTTPStatusError as error:
            typer.echo(
                f"The server rejected the token (HTTP {error.response.status_code}).", err=True
            )
            raise typer.Exit(1) from None
        except httpx.HTTPError as error:
            typer.echo(f"Could not reach {api_url}: {error}", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Authenticated for {repository}.")

    if not save_token(root, token):
        typer.echo(
            "No system keychain available: set RASTRO_TOKEN in your environment instead.",
            err=True,
        )
    save_config(root, Config(api_url=api_url))
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
            "Install it with `pipx install rastro` or `uv tool install rastro`.",
            err=True,
        )


def _verify(token: str, api_url: str) -> str:
    from rastro_cli import client

    return str(client.whoami(token, api_url)["full_name"])


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
