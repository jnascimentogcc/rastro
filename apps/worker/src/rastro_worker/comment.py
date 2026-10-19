"""The single pull request comment Rastro keeps up to date (spec section 7.7)."""

from dataclasses import dataclass, field
from datetime import datetime

from rastro_worker.preview import PreviewItem

MARKER = "<!-- rastro:pr-impact -->"
MAX_VISIBLE_PROPOSALS = 5  # more would tire the reviewer; the rest sit in a collapsed block

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
        "new_rule": "New rule",
        "changes_rule": "Changes an existing rule",
        "was": "Currently",
        "validated": "Changes a rule validated by {who}{date}",
        "on": " on {date}",
        "unknown_why": "origin unknown (the conversation did not say why)",
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
        "new_rule": "Nova regra",
        "changes_rule": "Altera uma regra existente",
        "was": "Atualmente",
        "validated": "Altera uma regra validada por {who}{date}",
        "on": " em {date}",
        "unknown_why": "origem desconhecida (a conversa não disse porquê)",
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
    labels = _LABELS.get(language, _LABELS["en"])
    blocks: list[str] = []
    if findings:
        blocks.append(labels["title"])
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
    elif all_clear_sha:
        blocks.append(f"{labels['clear']}\n\n{labels['clear_body'].format(sha=all_clear_sha[:7])}")
    if proposals:
        blocks.append(_render_proposals(proposals, labels))
    if unavailable:
        blocks.append(f"_{labels.get('unavailable_' + unavailable, labels['unavailable_budget'])}_")
    return MARKER + "\n" + "\n\n".join(blocks) + "\n"


def _render_proposals(items: list[PreviewItem], labels: dict[str, str]) -> str:
    visible, hidden = items[:MAX_VISIBLE_PROPOSALS], items[MAX_VISIBLE_PROPOSALS:]
    parts = [f"{labels['proposals_title']}\n\n_{labels['proposals_note']}_"]
    parts.append("\n\n".join(_render_proposal(item, labels) for item in visible))
    if hidden:
        body = "\n\n".join(_render_proposal(item, labels) for item in hidden)
        summary = labels["more"].format(n=len(hidden))
        parts.append(f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>")
    return "\n\n".join(parts)


def _render_proposal(item: PreviewItem, labels: dict[str, str]) -> str:
    kind = labels["new_rule"] if item.action == "create" else labels["changes_rule"]
    where = f" · `{item.symbol}`" if item.symbol else ""
    lines = [
        f"- **{item.statement}**",
        f"  {labels['why']}: {item.rationale or labels['unknown_why']}",
        f"  {kind}{where}",
    ]
    if item.replaces and item.replaces != item.statement:
        lines.append(f"  {labels['was']}: {item.replaces}")
    if item.validated_by:
        when = (
            labels["on"].format(date=item.validated_at.strftime(labels["date"]))
            if item.validated_at
            else ""
        )
        lines.append(f"  ⚠ {labels['validated'].format(who=item.validated_by, date=when)}")
    return "\n".join(lines)


def render_all_clear(head_sha: str, language: str = "en", *, flagged: bool = True) -> str:
    labels = _LABELS.get(language, _LABELS["en"])
    key = "clear_body" if flagged else "empty_body"
    body = labels[key].format(sha=head_sha[:7])
    return f"{MARKER}\n{labels['clear']}\n\n{body}\n"


def _origin(finding: Finding, labels: dict[str, str]) -> str | None:
    if not finding.origin_commit:
        return None
    sha = finding.origin_commit[:7]
    if finding.origin_date is None:
        return labels["commit"].format(sha=sha)
    return labels["request_of"].format(date=finding.origin_date.strftime(labels["date"]), sha=sha)
