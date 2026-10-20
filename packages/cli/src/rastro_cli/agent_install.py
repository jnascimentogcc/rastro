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


MCP_SERVER = "rastro"
MCP_MODULE = "rastro_mcp"
MCP_SCRIPT = "rastro-mcp"
MCP_PERMISSION = "mcp__rastro"  # allow-list entry covering all three read-only tools


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


# ---------------------------------------------------------------------- MCP server (.mcp.json)


def mcp_path(root: Path) -> Path:
    return root / ".mcp.json"


def _mcp_entry(*, shared: bool, python: str) -> dict[str, Any]:
    if shared:
        return {"type": "stdio", "command": MCP_SCRIPT}
    return {"type": "stdio", "command": python, "args": ["-m", MCP_MODULE]}


def _mcp_is_ours(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    return entry.get("command") == MCP_SCRIPT or (isinstance(args, list) and MCP_MODULE in args)


def _set_enabled_server(root: Path, *, enabled: bool) -> None:
    """Pre-approve (or stop approving) the server in the local settings, so Claude Code does not
    ask about a `.mcp.json` entry the person just installed on purpose."""
    path = settings_path(root, shared=False)
    settings = _load(path)
    names = settings.get("enabledMcpjsonServers", [])
    if not isinstance(names, list):
        raise AgentInstallError(f"{path}: `enabledMcpjsonServers` is not a list; fix it first")
    updated = [name for name in names if name != MCP_SERVER]
    if enabled:
        updated.append(MCP_SERVER)
    if updated:
        settings["enabledMcpjsonServers"] = updated
    else:
        settings.pop("enabledMcpjsonServers", None)
    if settings:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def install_mcp(root: Path, *, shared: bool, python: str) -> str:
    """Add the `rastro` server to `.mcp.json`. Returns `created`, `updated` or `unchanged`.

    Local: the entry holds this machine's interpreter, so the file must not be a team file that
    already exists; it is refused unless it holds nothing but our entry. Shared: the entry is
    the `rastro-mcp` script, and other servers in the file are kept. A `rastro` server that is
    not ours is never overwritten.
    """
    path = mcp_path(root)
    existed = path.exists()
    config = _load(path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise AgentInstallError(f"{path}: `mcpServers` is not an object; fix it first")
    current = servers.get(MCP_SERVER)
    if current is not None and not _mcp_is_ours(current):
        raise AgentInstallError(
            f"{path} already defines a `{MCP_SERVER}` server that is not Rastro's"
        )
    if not shared and existed and (set(config) != {"mcpServers"} or set(servers) - {MCP_SERVER}):
        raise AgentInstallError(
            f"{path} already exists and may be committed, so a machine-specific entry does not belong "
            f"in it: use --shared, or `claude mcp add --scope local {MCP_SERVER} -- {python} -m {MCP_MODULE}`"
        )
    wanted = _mcp_entry(shared=shared, python=python)
    if current == wanted:
        result = "unchanged"
    else:
        servers[MCP_SERVER] = wanted
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        result = "updated" if existed else "created"
    if not shared:
        _set_enabled_server(root, enabled=True)
    return result


def uninstall_mcp(root: Path) -> str:
    """Remove our server (and its local approval). Returns `removed` or `nothing`."""
    path = mcp_path(root)
    removed = False
    if path.exists():
        config = _load(path)
        servers = config.get("mcpServers")
        if isinstance(servers, dict) and _mcp_is_ours(servers.get(MCP_SERVER)):
            del servers[MCP_SERVER]
            removed = True
            if not servers:
                del config["mcpServers"]
            if config:
                path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            else:
                path.unlink()
    local = settings_path(root, shared=False)
    if local.exists() and MCP_SERVER in (_load(local).get("enabledMcpjsonServers") or []):
        _set_enabled_server(root, enabled=False)
        removed = True
    return "removed" if removed else "nothing"
