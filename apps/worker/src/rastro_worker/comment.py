"""The single pull request comment Rastro keeps up to date (spec section 7.7).

It has up to three parts: the impact warnings (or the all-clear), the rules the PR proposes (with
a checkbox each, phase 9.3) and a notice when the preview could not run. They are rendered
separately so a review can rebuild the comment without calling the model again.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from rastro_github import COMMENT_MARKER
from rastro_worker.preview import PreviewItem

MARKER = COMMENT_MARKER
MAX_VISIBLE_PROPOSALS = 5  # more would tire the reviewer; the rest sit in a collapsed block
PROPOSAL_MARKER = "<!-- rastro:proposal:{key} -->"
SHORT_ID = 6  # characters of a proposal key shown to people, enough for `/rastro reject <id>`

_LABELS = {
    "en": {
        "title": "### Rastro — rules affected by this PR",
        "clear": "### Rastro — no rules affected by this PR",
        "clear_body": "The rules flagged earlier are no longer affected as of `{sha}`.",
        "why": "Why",
        "unknown": "origin unknown",
        "risk": "Risk",
        "warning": "Warning: the same logic exists in {files} and was not changed.",
        "origin": "Origin",
        "request_of": "request of {date}, commit {sha}",
        "commit": "commit {sha}",
        "date": "%m/%d",
        "empty_body": "No rules are affected and none is proposed as of `{sha}`.",
        "proposals_title": "### Rastro — rules this PR proposes",
        "proposals_note": "Read from this PR's sessions. Nothing is saved until the PR is merged.",
        "actions": "Check a box to confirm a rule. Other actions, as a new comment: "
        "`/rastro reject <id>` · `/rastro correct <id>: <new statement>` · "
        "`/rastro why <id> <reason>` · `/rastro ok <id>`",
        "new_rule": "New rule",
        "changes_rule": "Changes an existing rule",
        "was": "Currently",
        "validated": "Changes a rule validated by {who}{date}",
        "on": " on {date}",
        "unknown_why": "origin unknown (the conversation did not say why)",
        "human_why": "written by {who}",
        "confirmed_by": "confirmed by {who}",
        "corrected_by": "corrected by {who}",
        "rejected_by": "rejected by {who}",
        "more": "{n} more",
        "unavailable_budget": "Rule preview unavailable: the monthly cost limit was reached.",
        "unavailable_quota": "Rule preview unavailable: the model account has no credit left.",
    },
    "pt": {
        "title": "### Rastro — regras afetadas por este PR",
        "clear": "### Rastro — nenhuma regra afetada por este PR",
        "clear_body": "As regras assinaladas antes já não são afetadas em `{sha}`.",
        "why": "Porquê",
        "unknown": "origem desconhecida",
        "risk": "Risco",
        "warning": "Atenção: a mesma lógica existe em {files} e não foi alterada.",
        "origin": "Origem",
        "request_of": "pedido de {date}, commit {sha}",
        "commit": "commit {sha}",
        "date": "%d/%m",
        "empty_body": "Nenhuma regra é afetada e nenhuma é proposta em `{sha}`.",
        "proposals_title": "### Rastro — regras que este PR propõe",
        "proposals_note": "Lidas das sessões deste PR. Nada é guardado até o PR ser integrado.",
        "actions": "Marque a caixa para confirmar uma regra. Outras ações, num novo comentário: "
        "`/rastro reject <id>` · `/rastro correct <id>: <nova frase>` · "
        "`/rastro why <id> <porquê>` · `/rastro ok <id>`",
        "new_rule": "Nova regra",
        "changes_rule": "Altera uma regra existente",
        "was": "Atualmente",
        "validated": "Altera uma regra validada por {who}{date}",
        "on": " em {date}",
        "unknown_why": "origem desconhecida (a conversa não disse porquê)",
        "human_why": "escrito por {who}",
        "confirmed_by": "confirmada por {who}",
        "corrected_by": "corrigida por {who}",
        "rejected_by": "rejeitada por {who}",
        "more": "mais {n}",
        "unavailable_budget": "Pré-visualização de regras indisponível: o limite mensal de custo foi atingido.",
        "unavailable_quota": "Pré-visualização de regras indisponível: a conta do modelo ficou sem crédito.",
    },
}


@dataclass(frozen=True)
class Finding:
    rule_id: str
    statement: str
    rationale: str | None
    risk: str
    confidence: float
    explanation: str
    unchanged_duplicates: list[str] = field(default_factory=list)  # file paths of untouched copies
    origin_commit: str | None = None
    origin_date: datetime | None = None


_MARKDOWN_SPECIALS = re.compile(r"([\\`*_\[\]<>~|#])")


def escape_markdown(text: str) -> str:
    """Text a person (or a model) wrote, shown back inside Markdown: no formatting, links, HTML or
    hidden comments can come out of it, and it stays on one line."""
    return _MARKDOWN_SPECIALS.sub(r"\\\1", " ".join(text.split()))


def render_comment(
    findings: list[Finding],
    language: str = "en",
    *,
    proposals: list[PreviewItem] | None = None,
    unavailable: str | None = None,
    all_clear_sha: str | None = None,
) -> str:
    """The impact warnings, then (when there are any) the rules this PR proposes.

    `all_clear_sha` turns the impact part into "no rules affected" after an earlier warning went
    away. Every part is optional; the marker always comes first so the comment can be found.
    """
    return compose_comment(
        render_impact_section(findings, language, all_clear_sha=all_clear_sha),
        render_proposals_section(proposals or [], language),
        render_notice(unavailable, language),
    )


def compose_comment(impact: str, proposals: str, notice: str) -> str:
    parts = [part for part in (impact, proposals, notice) if part]
    return MARKER + "\n" + "\n\n".join(parts) + "\n"


def render_impact_section(
    findings: list[Finding], language: str = "en", *, all_clear_sha: str | None = None
) -> str:
    labels = _LABELS.get(language, _LABELS["en"])
    if not findings:
        return render_all_clear_section(all_clear_sha, language) if all_clear_sha else ""
    blocks = [labels["title"]]
    for finding in findings:
        lines = [
            f"**{finding.statement}**",
            f"{labels['why']}: {finding.rationale or labels['unknown']}",
            f"{labels['risk']}: {finding.explanation}",
        ]
        if finding.unchanged_duplicates:
            files = ", ".join(f"`{path}`" for path in finding.unchanged_duplicates)
            lines.append(labels["warning"].format(files=files))
        origin = _origin(finding, labels)
        if origin:
            lines.append(f"{labels['origin']}: {origin}.")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_all_clear_section(head_sha: str, language: str = "en", *, flagged: bool = True) -> str:
    labels = _LABELS.get(language, _LABELS["en"])
    key = "clear_body" if flagged else "empty_body"
    return f"{labels['clear']}\n\n{labels[key].format(sha=head_sha[:7])}"


def render_all_clear(head_sha: str, language: str = "en", *, flagged: bool = True) -> str:
    return f"{MARKER}\n{render_all_clear_section(head_sha, language, flagged=flagged)}\n"


def render_notice(unavailable: str | None, language: str = "en") -> str:
    if not unavailable:
        return ""
    labels = _LABELS.get(language, _LABELS["en"])
    return f"_{labels.get('unavailable_' + unavailable, labels['unavailable_budget'])}_"


def render_proposals_section(items: list[PreviewItem], language: str = "en") -> str:
    if not items:
        return ""
    labels = _LABELS.get(language, _LABELS["en"])
    visible, hidden = items[:MAX_VISIBLE_PROPOSALS], items[MAX_VISIBLE_PROPOSALS:]
    parts = [f"{labels['proposals_title']}\n\n_{labels['proposals_note']}_"]
    parts.append("\n\n".join(_render_proposal(item, labels) for item in visible))
    if hidden:
        body = "\n\n".join(_render_proposal(item, labels) for item in hidden)
        summary = labels["more"].format(n=len(hidden))
        parts.append(f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>")
    parts.append(f"_{labels['actions']}_")
    return "\n\n".join(parts)


def _render_proposal(item: PreviewItem, labels: dict[str, str]) -> str:
    statement = escape_markdown(item.statement)
    marker = PROPOSAL_MARKER.format(key=item.key)
    who = escape_markdown(item.reviewed_by or "")
    if item.state == "rejected":
        return f"- ~~{statement}~~ — {labels['rejected_by'].format(who=who)} {marker}"

    kind = labels["new_rule"] if item.action == "create" else labels["changes_rule"]
    where = f" · `{item.symbol}`" if item.symbol else ""
    why = escape_markdown(item.rationale) if item.rationale else labels["unknown_why"]
    if item.rationale and item.rationale_source == "human" and item.rationale_by:
        why += f" ({labels['human_why'].format(who=escape_markdown(item.rationale_by))})"
    box = "x" if item.state in ("confirmed", "corrected") else " "
    lines = [
        f"- [{box}] **{statement}** {marker}",
        f"  {labels['why']}: {why}",
        f"  {kind}{where} · id `{item.key[:SHORT_ID]}`",
    ]
    if item.replaces and item.replaces != item.statement:
        lines.append(f"  {labels['was']}: {escape_markdown(item.replaces)}")
    if item.validated_by:
        when = (
            labels["on"].format(date=item.validated_at.strftime(labels["date"]))
            if item.validated_at
            else ""
        )
        lines.append(
            f"  ⚠ {labels['validated'].format(who=escape_markdown(item.validated_by), date=when)}"
        )
    if item.state == "confirmed":
        lines.append(f"  ✔ {labels['confirmed_by'].format(who=who)}")
    elif item.state == "corrected":
        lines.append(f"  ✔ {labels['corrected_by'].format(who=who)}")
    return "\n".join(lines)


def _origin(finding: Finding, labels: dict[str, str]) -> str | None:
    if not finding.origin_commit:
        return None
    sha = finding.origin_commit[:7]
    if finding.origin_date is None:
        return labels["commit"].format(sha=sha)
    return labels["request_of"].format(date=finding.origin_date.strftime(labels["date"]), sha=sha)
