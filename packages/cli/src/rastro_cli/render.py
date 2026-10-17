"""Plain-text output of `rastro rules` / `rastro rule`."""

import textwrap
from datetime import datetime
from typing import Any

WIDTH = 100


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y")
    except ValueError:
        return None


def _origin(origin: dict[str, Any] | None) -> str | None:
    if not origin:
        return None
    since = f"commit {origin['commit_sha'][:7]}"
    date = _date(origin.get("committed_at"))
    return f"{since} ({date})" if date else since


def _wrap(prefix: str, text: str) -> str:
    return textwrap.fill(
        text, width=WIDTH, initial_indent=prefix, subsequent_indent=" " * len(prefix)
    )


def render_rules(payload: dict[str, Any], *, status: str, search: str | None = None) -> str:
    items: list[dict[str, Any]] = payload["items"]
    total: int = payload["total"]
    if not items:
        scope = f' matching "{search}"' if search else ""
        adjective = "" if status == "all" else f"{status} "
        return f"No {adjective}rules found{scope}.\n"

    kind = {"semantic": "semantic search", "text": "text search (no embedding model)"}.get(
        payload.get("search") or "", ""
    )
    header = f"{len(items)} of {total} rule(s) [{status}]"
    if search:
        header += f' for "{search}"' + (f" - {kind}" if kind else "")
    blocks = [header, ""]
    for number, rule in enumerate(items, start=1):
        marker = "" if rule["status"] == "active" else f" [{rule['status']}]"
        score = f"  ({rule['similarity']:.0%} match)" if rule.get("similarity") is not None else ""
        blocks.append(_wrap(f"{number:>2}. ", rule["statement"] + marker + score))
        why = rule["rationale"] or "unknown origin (the conversation did not say why)"
        blocks.append(_wrap("    Why: ", why))
        if rule["files"]:
            blocks.append(_wrap("    Files: ", ", ".join(rule["files"])))
        origin = _origin(rule.get("origin"))
        tail = f"id {rule['id'][:8]}"
        blocks.append(f"    Since: {origin} - {tail}" if origin else f"    {tail}")
        blocks.append("")
    if total > len(items):
        blocks.append(f"{total - len(items)} more; raise --limit to see them.")
    return "\n".join(blocks).rstrip() + "\n"


def render_rule(rule: dict[str, Any]) -> str:
    lines = [
        _wrap("", rule["statement"]),
        "",
        f"id:         {rule['id']}",
        f"status:     {rule['status']}  (confidence {rule['confidence']:.0%})",
        _wrap(
            "why:        ", rule["rationale"] or "unknown origin (not stated in the conversation)"
        ),
    ]
    if rule["anchors"]:
        lines += ["", "code:"]
        for anchor in rule["anchors"]:
            span = (
                f":{anchor['start_line']}-{anchor['end_line']}"
                if anchor.get("start_line") and anchor["status"] == "active"
                else ""
            )
            note = "" if anchor["status"] == "active" else "  (removed)"
            lines.append(
                f"  {anchor['role']:<9} {anchor['file_path']}{span}  {anchor['qualified_name']}{note}"
            )
    if rule["origins"]:
        lines += ["", "history:"]
        for origin in rule["origins"]:
            when = _date(origin.get("committed_at"))
            lines.append(f"  commit {origin['commit_sha'][:7]}" + (f"  {when}" if when else ""))
            if origin.get("quote"):
                lines.append(_wrap('    "', origin["quote"] + '"'))
    return "\n".join(lines) + "\n"
