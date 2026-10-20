"""Install the Rastro hooks into Claude Code's settings (phase 10.2).

Local by default (`.claude/settings.local.json`, which Claude Code keeps out of git): hooks in a
shared file run commands on every teammate's machine, so `--shared` (`.claude/settings.json`) is
opt-in and uses the `rastro-hook` script instead of a machine-specific interpreter path.

Merging never touches hooks that are not ours: ours are recognised by running `rastro_cli.hook_entry`
or by being the `rastro-hook` script. Installing twice changes nothing; uninstalling removes only
what was installed.
"""

import json
from pathlib import Path
from typing import Any

HOOK_MODULE = "rastro_cli.hook_entry"
HOOK_SCRIPT = "rastro-hook"
TOOL_MATCHER = "Read|Edit|Write|MultiEdit"
PRE_TOOL_TIMEOUT_SECONDS = 3
SESSION_START_TIMEOUT_SECONDS = 10


class AgentInstallError(RuntimeError):
    pass


def settings_path(root: Path, *, shared: bool) -> Path:
    return root / ".claude" / ("settings.json" if shared else "settings.local.json")


def _hook(subcommand: str, timeout: int, *, shared: bool, python: str) -> dict[str, Any]:
    if shared:
        return {"type": "command", "command": HOOK_SCRIPT, "args": [subcommand], "timeout": timeout}
    return {
        "type": "command",
        "command": python,
        "args": ["-m", HOOK_MODULE, subcommand],
        "timeout": timeout,
    }


def desired_groups(*, shared: bool, python: str) -> dict[str, dict[str, Any]]:
    return {
        "SessionStart": {
            "hooks": [
                _hook("session-start", SESSION_START_TIMEOUT_SECONDS, shared=shared, python=python)
            ]
        },
        "PreToolUse": {
            "matcher": TOOL_MATCHER,
            "hooks": [_hook("pre-tool", PRE_TOOL_TIMEOUT_SECONDS, shared=shared, python=python)],
        },
    }


def is_ours(hook: Any) -> bool:
    if not isinstance(hook, dict):
        return False
    args = hook.get("args")
    return hook.get("command") == HOOK_SCRIPT or (isinstance(args, list) and HOOK_MODULE in args)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise AgentInstallError(
            f"{path} is not valid JSON ({error}); fix or remove it first"
        ) from None
    if not isinstance(data, dict):
        raise AgentInstallError(f"{path} does not hold a JSON object; fix or remove it first")
    return data


def _strip_ours(groups: list[Any]) -> list[Any]:
    """The groups without our hooks; a group that only held ours disappears."""
    kept: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        others = [hook for hook in group["hooks"] if not is_ours(hook)]
        if len(others) == len(group["hooks"]):
            kept.append(group)
        elif others:
            kept.append({**group, "hooks": others})
    return kept


def install(root: Path, *, shared: bool, python: str) -> str:
    """Returns `created`, `updated` or `unchanged`."""
    path = settings_path(root, shared=shared)
    existed = path.exists()
    settings = _load(path)
    before = json.dumps(settings, sort_keys=True)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentInstallError(f"{path}: `hooks` is not an object; fix it first")
    for event, wanted in desired_groups(shared=shared, python=python).items():
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise AgentInstallError(f"{path}: `hooks.{event}` is not a list; fix it first")
        if [group for group in groups if _has_ours(group)] == [wanted]:
            continue  # exactly our group and nothing else of ours: already installed
        hooks[event] = [*_strip_ours(groups), wanted]
    if json.dumps(settings, sort_keys=True) == before and existed:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return "updated" if existed else "created"


def _has_ours(group: Any) -> bool:
    return isinstance(group, dict) and any(is_ours(hook) for hook in group.get("hooks") or [])


def uninstall(root: Path, *, shared: bool) -> str:
    """Returns `removed` or `nothing`. A settings file left empty is deleted."""
    path = settings_path(root, shared=shared)
    if not path.exists():
        return "nothing"
    settings = _load(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return "nothing"
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        stripped = _strip_ours(groups)
        if stripped != groups:
            changed = True
            if stripped:
                hooks[event] = stripped
            else:
                del hooks[event]
    if not changed:
        return "nothing"
    if not hooks:
        del settings["hooks"]
    if settings:
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    else:
        path.unlink()
    return "removed"
