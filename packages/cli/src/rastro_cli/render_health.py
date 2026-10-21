"""Plain-text output of `rastro health`."""

import textwrap
from typing import Any

WIDTH = 100
REASON_TEXT = {
    "no_rationale": "no reason recorded",
    "not_validated": "not confirmed by a person",
    "validation_outdated": "confirmation outdated by an edit",
    "validation_outdated_by_code": "the code changed after it was confirmed",
}


def _percent(metric: dict[str, Any]) -> str:
    if not metric["available"]:
        return f"n/a ({metric['na_reason']})"
    text = f"{metric['numerator']} of {metric['denominator']} ({round(metric['value'] * 100)}%)"
    return text + (" - small sample" if metric["small_sample"] else "")


def _was(metric: dict[str, Any], earlier: dict[str, Any] | None) -> str:
    """What the same figure was in the snapshot the report is compared with, when it differs."""
    before = (earlier or {}).get("metrics", {}).get(metric["id"])
    if before is None or not before["available"] or not metric["available"]:
        return ""
    if (before["numerator"], before["denominator"]) == (metric["numerator"], metric["denominator"]):
        return ""
    return f" (was {before['numerator']} of {before['denominator']})"


def _explain(definition: dict[str, str] | None, indent: str = "      ") -> list[str]:
    if not definition:
        return []
    lines = [
        textwrap.fill(
            f"how: {definition['formula']}",
            WIDTH,
            initial_indent=indent,
            subsequent_indent=indent + "     ",
        ),
        textwrap.fill(
            f"from: {definition['source']}",
            WIDTH,
            initial_indent=indent,
            subsequent_indent=indent + "      ",
        ),
    ]
    if definition.get("not_proof"):
        lines.append(
            textwrap.fill(
                f"does not prove: {definition['not_proof']}",
                WIDTH,
                initial_indent=indent,
                subsequent_indent=indent + "                ",
            )
        )
    return lines


def render_health(report: dict[str, Any], *, zones: bool, explain: bool) -> str:
    definitions: dict[str, Any] = report.get("definitions") or {}
    earlier = report.get("compared_to")

    def pct(metric: dict[str, Any]) -> str:
        return _percent(metric) + _was(metric, earlier)

    metrics = {metric["id"]: metric for metric in report["knowledge"]}
    window = report["window_days"]
    head = (
        f"rules of {report['head_sha'][:7]}"
        if report.get("head_sha")
        else "no default-branch commit known"
    )
    out = [f"Rastro health  ({head}; pipeline problems of the last {window} days)", ""]
    if earlier:
        out += [
            f"Changes are against the snapshot of {earlier['taken_on']} ({earlier['days_ago']} days ago).",
            "",
        ]
    for warning in report["warnings"]:
        out += [textwrap.fill(f"! {warning}", WIDTH, subsequent_indent="  "), ""]
    if explain and report["warnings"]:
        out += _explain(definitions.get("V2")) + [""]

    r1 = metrics["R1"]["detail"]
    others = [f"{r1[key]} {key}" for key in ("orphaned", "rejected", "superseded") if r1[key]]
    out.append("Knowledge base (current state)")
    out.append(
        f"  Active rules                    {r1['active']}"
        + (f"   ({', '.join(others)})" if others else "")
    )
    out += _explain(definitions.get("R1")) if explain else []

    r2 = metrics["R2"]
    line = f"  With a known reason             {pct(r2)}"
    if r2["available"]:
        line += (
            f"   {r2['detail']['from_conversation']} from conversations, "
            f"{r2['detail']['written_by_people']} written by people"
        )
    out.append(line)
    out += _explain(definitions.get("R2")) if explain else []

    out.append(f"  Confirmed by a person           {pct(metrics['R3'])}")
    out += _explain(definitions.get("R3")) if explain else []
    r3b = metrics["R3b"]
    out.append(f"    ...verified by GitHub         {pct(r3b)}")
    out += _explain(definitions.get("R3b")) if explain else []
    r3a = metrics.get("R3a")
    if r3a is not None:
        out.append(f"    ...by someone other than the PR author  {pct(r3a)}")
        out += _explain(definitions.get("R3a")) if explain else []

    out.append(f"  Confirmation outdated by edits  {pct(metrics['R4'])}")
    out += _explain(definitions.get("R4")) if explain else []

    r5 = metrics.get("R5")
    if r5 is not None:
        out.append(f"  Code changed after confirming   {pct(r5)}")
        out += _explain(definitions.get("R5")) if explain else []

    r6 = metrics["R6"]
    if r6["available"]:
        out.append(
            f"  Oldest unconfirmed rule         {r6['detail']['oldest_days']} days "
            f"(median {r6['detail']['median_days']}), {r6['numerator']} rule(s) waiting"
        )
    else:
        out.append(f"  Oldest unconfirmed rule         n/a ({r6['na_reason']})")
    out += _explain(definitions.get("R6")) if explain else []

    process = {metric["id"]: metric for metric in report.get("process", [])}
    if process:
        out += ["", f"Process (last {window} days)"]
        out.append(f"  Pull requests with proposals    {process['P1']['numerator']}")
        out += _explain(definitions.get("P1")) if explain else []
        out.append(f"  Reviewed before the merge       {pct(process['P2'])}")
        out += _explain(definitions.get("P2")) if explain else []
        out.append(f"  Integrated without review       {pct(process['P3'])}")
        out += _explain(definitions.get("P3")) if explain else []
        p4 = process["P4"]
        if p4["available"]:
            out.append(
                f"  Time to first review            {p4['detail']['median_hours']} h (median of "
                f"{p4['numerator']} proposal(s))"
            )
        else:
            out.append(f"  Time to first review            n/a ({p4['na_reason']})")
        out += _explain(definitions.get("P4")) if explain else []
        p5 = process["P5"]
        if p5["available"]:
            d = p5["detail"]
            out.append(
                f"  Verdicts                        {pct(p5)} confirmed as written, "
                f"{d['corrected']} corrected, {d['rejected']} rejected"
            )
        else:
            out.append(f"  Verdicts                        n/a ({p5['na_reason']})")
        out += _explain(definitions.get("P5")) if explain else []

    agents = {metric["id"]: metric for metric in report.get("agents", [])}
    if agents:
        out += ["", f"Agents (last {window} days)"]
        c1 = agents["C1"]
        out.append(f"  Files with rules announced first  {pct(c1)}")
        if c1["available"]:
            with_hook, without = c1["detail"]["with_hook"], c1["detail"]["without_hook"]
            out.append(
                f"    with the hook {with_hook['announced']} of {with_hook['edited']}, "
                f"without it {without['announced']} of {without['edited']}"
            )
        out += _explain(definitions.get("C1")) if explain else []
        c2 = agents["C2"]
        line = f"  Sessions where the hook ran       {pct(c2)}"
        if c2["available"] and c2["detail"].get("unknown"):
            line += f"   ({c2['detail']['unknown']} session(s) did not say)"
        out.append(line)
        out += _explain(definitions.get("C2")) if explain else []

    for v1 in report.get("intent", []):
        out += ["", f"Code without recorded intent (last {window} days)"]
        out.append(f"  Integrations with no session    {pct(v1)}")
        loose = v1["detail"].get("unattributed_commits", 0)
        if loose:
            out.append(
                f"    {loose} commit(s) with no session and no pull request are left out: they cannot be "
                "told apart from older merge commits"
            )
        out += _explain(definitions.get("V1")) if explain else []

    if zones:
        out += ["", "Zones (directories; tests, dependencies and generated code left out)"]
        if not report["zones"]:
            out.append("  no zones")
        for zone in report["zones"]:
            share = (
                f"{round(zone['symbols_with_rule'] / zone['symbols'] * 100)}%"
                if zone["symbols"]
                else "n/a"
            )
            out.append(
                f"  {zone['name']:<28} symbols {zone['symbols']:>4}, with a rule {zone['symbols_with_rule']:>3} "
                f"({share}), rules {zone['rules']:>3}, confirmed {zone['validated']:>3}, "
                f"no reason {zone['without_reason']:>3}"
            )
        out += _explain(definitions.get("zones"), "  ") if explain else []

    if report["queue"] or explain:
        out += ["", "Review queue (what to confirm first)"]
        if not report["queue"]:
            out.append("  nothing waiting")
        for number, item in enumerate(report["queue"], 1):
            why = [REASON_TEXT.get(reason, reason) for reason in item["reasons"]]
            if item["reasons"] == ["no_rationale"]:
                why = [
                    "confirmed; only the reason is missing (writing it asks for a new confirmation)"
                ]
            if item["callers"]:
                why.append(f"{item['callers']} caller(s)")
            statement = textwrap.shorten(item["statement"], 60, placeholder="...")
            out.append(f"  {number}. {item['rule_id'][:6]}  {statement}")
            out.append(f"       {'; '.join(why)}")
            out.append(f"       {item['command']}")
        out += _explain(definitions.get("queue"), "  ") if explain else []
    return "\n".join(out) + "\n"


def render_history(history: dict[str, Any]) -> str:
    """One line per snapshot, most recent first, with the figures a trend is read from."""
    snapshots = history["snapshots"]
    if not snapshots:
        return "No snapshots yet: the worker takes one a day.\n"

    def cell(figure: dict[str, Any] | None) -> str:
        if figure is None or not figure["available"]:
            return "n/a"
        return f"{figure['numerator']}/{figure['denominator']}"

    columns = [("R2", "reason"), ("R3", "confirmed"), ("R4", "edited"), ("R5", "code changed")]
    out = ["Rastro health history  (figures as of each day, most recent first)", ""]
    out.append("  date        rules  " + "  ".join(f"{name:>12}" for _, name in columns))
    for snapshot in snapshots:
        figures = snapshot["figures"]
        rules = figures.get("R1", {}).get("numerator")
        cells = "  ".join(f"{cell(figures.get(metric_id)):>12}" for metric_id, _ in columns)
        out.append(f"  {snapshot['taken_on']}  {rules if rules is not None else '?':>5}  {cells}")
    versions = {snapshot["definitions_version"] for snapshot in snapshots}
    if len(versions) > 1:
        out += [
            "",
            "! The figures changed definition inside this period: do not read it as a trend.",
        ]
    return "\n".join(out) + "\n"
