"""The single pull request comment Rastro keeps up to date (spec section 7.7)."""

from dataclasses import dataclass, field
from datetime import datetime

MARKER = "<!-- rastro:pr-impact -->"

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


def render_comment(findings: list[Finding], language: str = "en") -> str:
    labels = _LABELS.get(language, _LABELS["en"])
    blocks = [f"{MARKER}\n{labels['title']}"]
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
    return "\n\n".join(blocks) + "\n"


def render_all_clear(head_sha: str, language: str = "en") -> str:
    labels = _LABELS.get(language, _LABELS["en"])
    body = labels["clear_body"].format(sha=head_sha[:7])
    return f"{MARKER}\n{labels['clear']}\n\n{body}\n"


def _origin(finding: Finding, labels: dict[str, str]) -> str | None:
    if not finding.origin_commit:
        return None
    sha = finding.origin_commit[:7]
    if finding.origin_date is None:
        return labels["commit"].format(sha=sha)
    return labels["request_of"].format(date=finding.origin_date.strftime(labels["date"]), sha=sha)
