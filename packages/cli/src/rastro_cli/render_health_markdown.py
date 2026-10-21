"""Markdown output of `rastro health`, for a pull request, a wiki or a message to someone else."""

from typing import Any

from rastro_cli.render_health import REASON_TEXT, _percent, _was


def _cell(text: object) -> str:
    """Rule text and reasons come from the server: keep them inside one table cell."""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _table(header: tuple[str, ...], rows: list[tuple[object, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines + [""]


def render_markdown(report: dict[str, Any], *, zones: bool) -> str:
    earlier = report.get("compared_to")

    def figure(metric: dict[str, Any]) -> str:
        return _percent(metric) + _was(metric, earlier)

    window = report["window_days"]
    knowledge = {metric["id"]: metric for metric in report["knowledge"]}
    head = report.get("head_sha")
    out = ["# Rastro health", ""]
    out.append(
        f"Rules of `{head[:7]}`; process and pipeline figures cover the last {window} days."
        if head
        else "No default-branch commit is known yet."
    )
    if earlier:
        out.append(
            f"Changes are against the snapshot of {earlier['taken_on']} ({earlier['days_ago']} days ago)."
        )
    out.append("")
    for warning in report["warnings"]:
        out += [f"> **Warning:** {_cell(warning)}", ""]

    r1 = knowledge["R1"]["detail"]
    others = [f"{r1[key]} {key}" for key in ("orphaned", "rejected", "superseded") if r1[key]]
    rows: list[tuple[object, ...]] = [
        ("Active rules", r1["active"], ", ".join(others)),
        ("With a known reason", figure(knowledge["R2"]), ""),
        ("Confirmed by a person", figure(knowledge["R3"]), ""),
        ("...verified by GitHub", figure(knowledge["R3b"]), ""),
    ]
    if "R3a" in knowledge:
        rows.append(("...by someone other than the PR author", figure(knowledge["R3a"]), ""))
    rows.append(("Confirmation outdated by edits", figure(knowledge["R4"]), ""))
    if "R5" in knowledge:
        rows.append(("Code changed after confirming", figure(knowledge["R5"]), ""))
    r6 = knowledge["R6"]
    rows.append(
        (
            "Oldest unconfirmed rule",
            f"{r6['detail']['oldest_days']} days (median {r6['detail']['median_days']})"
            if r6["available"]
            else f"n/a ({r6['na_reason']})",
            f"{r6['numerator']} rule(s) waiting" if r6["available"] else "",
        )
    )
    out += ["## Knowledge base", ""] + _table(("Figure", "Value", "Note"), rows)

    process = {metric["id"]: metric for metric in report.get("process", [])}
    if process:
        p4, p5 = process["P4"], process["P5"]
        rows = [
            ("Pull requests with proposals", process["P1"]["numerator"]),
            ("Reviewed before the merge", figure(process["P2"])),
            ("Integrated without review", figure(process["P3"])),
            (
                "Time to first review",
                f"{p4['detail']['median_hours']} h (median)"
                if p4["available"]
                else f"n/a ({p4['na_reason']})",
            ),
            (
                "Verdicts",
                f"{figure(p5)} confirmed as written, {p5['detail']['corrected']} corrected, "
                f"{p5['detail']['rejected']} rejected"
                if p5["available"]
                else f"n/a ({p5['na_reason']})",
            ),
        ]
        out += [f"## Process (last {window} days)", ""] + _table(("Figure", "Value"), rows)

    agents = {metric["id"]: metric for metric in report.get("agents", [])}
    if agents:
        c1, c2 = agents["C1"], agents["C2"]
        rows = [
            ("Files with rules announced first", figure(c1)),
            ("Sessions where the hook ran", figure(c2)),
        ]
        out += [f"## Agents (last {window} days)", ""] + _table(("Figure", "Value"), rows)

    for v1 in report.get("intent", []):
        note = v1["detail"].get("unattributed_commits", 0)
        out += [f"## Code without recorded intent (last {window} days)", ""]
        out += _table(("Figure", "Value"), [("Integrations with no session", figure(v1))])
        if note:
            out += [f"{note} commit(s) with no session and no pull request are left out.", ""]

    if zones:
        rows = [
            (
                zone["name"],
                zone["symbols"],
                zone["symbols_with_rule"],
                zone["rules"],
                zone["validated"],
                zone["without_reason"],
            )
            for zone in report["zones"]
        ]
        out += ["## Zones", ""]
        out += _table(("Zone", "Symbols", "With a rule", "Rules", "Confirmed", "No reason"), rows)

    if report["queue"]:
        out += ["## Review queue", ""]
        rows = []
        for number, item in enumerate(report["queue"], 1):
            why = [REASON_TEXT.get(reason, reason) for reason in item["reasons"]]
            if item["reasons"] == ["no_rationale"]:
                why = [
                    "confirmed; only the reason is missing (writing it asks for a new confirmation)"
                ]
            if item["callers"]:
                why.append(f"{item['callers']} caller(s)")
            rows.append((number, item["statement"], "; ".join(why), f"`{item['command']}`"))
        out += _table(("#", "Rule", "Why", "Do"), rows)
    return "\n".join(out).rstrip("\n") + "\n"
