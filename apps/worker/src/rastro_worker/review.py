"""Review of the proposed rules from GitHub itself (phase 9.3, spec D2).

Identity is the GitHub user of the webhook event, so there is no login system: ticking a box in
Rastro's comment confirms a proposal, and `/rastro ...` comments reject, correct or explain it.
Only people with write access to the repository count, checked against GitHub on every action;
everybody else is ignored in silence. The reviewer's text is data: it is capped, never sent to a
model and escaped when shown again.

Nothing here touches `rules`: reviews only change `rule_proposals`. Phase 9.4 applies them when the
PR is merged.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from rastro_db.models import PrAnalysis, Repository, RuleProposal
from rastro_github import CommentNotFoundError
from rastro_worker.comment import (
    PROPOSAL_MARKER,
    compose_comment,
    render_notice,
    render_proposals_section,
)
from rastro_worker.deps import WorkerDeps
from rastro_worker.preview import load_review_items

logger = logging.getLogger(__name__)

WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
MAX_TEXT = 500
MIN_REF = 4  # characters of a proposal id that can be typed in a command
REVIEWABLE = ("pending", "confirmed", "corrected", "rejected")

_COMMAND = re.compile(r"^\s*/rastro\s+(?P<verb>\w+)(?P<rest>.*)$", re.DOTALL | re.IGNORECASE)
_BOX = re.compile(
    r"^- \[(?P<box>[ xX])\] .*?"
    + re.escape(PROPOSAL_MARKER).replace(r"\{key\}", r"(?P<key>[0-9a-f]+)"),
    re.MULTILINE,
)
_VERBS = {
    "ok": "confirm",
    "confirm": "confirm",
    "reject": "reject",
    "correct": "correct",
    "why": "why",
}

_REPLIES = {
    "en": {
        "usage": "Rastro: I did not understand that. Use `/rastro ok <id>`, `/rastro reject <id>`, "
        "`/rastro correct <id>: <new statement>` or `/rastro why <id> <reason>`.",
        "not_found": "Rastro: no proposal of this PR has an id starting with `{ref}`.",
        "ambiguous": "Rastro: `{ref}` matches more than one proposal; type more of the id.",
        "needs_text": "Rastro: `/rastro {verb}` needs some text after the id.",
    },
    "pt": {
        "usage": "Rastro: não percebi. Use `/rastro ok <id>`, `/rastro reject <id>`, "
        "`/rastro correct <id>: <nova frase>` ou `/rastro why <id> <porquê>`.",
        "not_found": "Rastro: nenhuma proposta deste PR tem um id que comece por `{ref}`.",
        "ambiguous": "Rastro: `{ref}` corresponde a mais de uma proposta; escreva mais do id.",
        "needs_text": "Rastro: `/rastro {verb}` precisa de texto depois do id.",
    },
}


@dataclass(frozen=True)
class ReviewAction:
    kind: str  # confirm | uncheck | reject | correct | why
    ref: str  # a proposal key, or the start of one typed by a person
    text: str | None = None


@dataclass(frozen=True)
class CommandError:
    reason: str  # usage | needs_text
    verb: str = ""


@dataclass
class ReviewResult:
    applied: int = 0
    ignored: str | None = None  # why nothing happened: not_allowed | not_ours | nothing_to_do | ...
    replied: bool = False
    updated_comment: bool = False


def clean_text(text: str) -> str:
    """One line, no control characters, at most MAX_TEXT characters."""
    printable = "".join(ch if ch.isprintable() or ch.isspace() else " " for ch in text)
    return " ".join(printable.split())[:MAX_TEXT]


def parse_command(body: str) -> ReviewAction | CommandError | None:
    """`/rastro ok|reject <id>`, `/rastro correct <id>: <text>`, `/rastro why <id> <text>`.

    None when the comment is not a Rastro command at all."""
    match = _COMMAND.match(body)
    if match is None:
        return None
    verb = _VERBS.get(match["verb"].lower())
    if verb is None:
        return CommandError("usage")
    parts = match["rest"].strip().split(None, 1)
    if not parts:
        return CommandError("usage")
    ref = parts[0].rstrip(":").lower()
    text = clean_text(parts[1].removeprefix(":") if len(parts) > 1 else "")
    if verb in ("correct", "why"):
        if not text:
            return CommandError("needs_text", match["verb"].lower())
        return ReviewAction(verb, ref, text)
    return ReviewAction(verb, ref)  # ok / reject: anything after the id is ignored


def box_states(body: str) -> dict[str, bool]:
    """proposal key -> is its checkbox ticked, for the proposals listed in a Rastro comment."""
    return {m["key"]: m["box"] in "xX" for m in _BOX.finditer(body)}


def checkbox_changes(previous: str, current: str) -> list[ReviewAction]:
    """The boxes that went from unticked to ticked (confirm) or the other way (uncheck)."""
    before, after = box_states(previous), box_states(current)
    return [
        ReviewAction("confirm" if after[key] else "uncheck", key)
        for key in after
        if key in before and before[key] != after[key]
    ]


async def apply_review(
    deps: WorkerDeps,
    repository_id: uuid.UUID,
    number: int,
    *,
    comment_id: int,
    sender: str,
    event_action: str,
    body: str,
    previous_body: str = "",
) -> ReviewResult:
    result = ReviewResult()
    async with deps.session_factory() as session:
        repository = await session.get(Repository, repository_id)
        analysis = await session.scalar(
            select(PrAnalysis).where(
                PrAnalysis.repository_id == repository_id, PrAnalysis.pr_number == number
            )
        )
    if repository is None or repository.installation_id == 0:
        result.ignored = "unknown_repository"
        return result
    language = deps.impact.impact_comment_language

    actions: list[ReviewAction | CommandError]
    if event_action == "edited":
        # Only Rastro's own comment counts: someone could paste the hidden markers elsewhere.
        if analysis is None or analysis.comment_id != comment_id:
            result.ignored = "not_ours"
            return result
        actions = list(checkbox_changes(previous_body, body))
    else:
        parsed = parse_command(body)
        actions = [parsed] if parsed is not None else []
    if not actions:
        result.ignored = "nothing_to_do"
        return result

    permission = await deps.github.get_collaborator_permission(
        repository.installation_id, repository.full_name, sender
    )
    if permission not in WRITE_PERMISSIONS:
        logger.info("review by %s ignored: permission %r is not write access", sender, permission)
        result.ignored = "not_allowed"
        return result

    replies: list[str] = []
    for action in actions:
        if isinstance(action, CommandError):
            replies.append(
                _REPLIES[language if language in _REPLIES else "en"][action.reason].format(
                    verb=action.verb
                )
            )
            continue
        problem = await _apply_one(deps, repository_id, number, sender, action)
        if problem is None:
            result.applied += 1
        elif event_action == "created":  # a mistyped command deserves an answer; a box does not
            template = _REPLIES[language if language in _REPLIES else "en"][problem]
            replies.append(template.format(ref=action.ref[:12], verb=action.kind))

    if result.applied and analysis is not None and analysis.comment_id is not None:
        result.updated_comment = await _rerender(deps, repository, analysis, number, language)
    for reply in replies:
        await deps.github.create_comment(
            repository.installation_id, repository.full_name, number, reply
        )
        result.replied = True
    return result


async def _apply_one(
    deps: WorkerDeps, repository_id: uuid.UUID, number: int, sender: str, action: ReviewAction
) -> str | None:
    """Apply one action; returns the reason it could not be applied, or None."""
    async with deps.session_factory() as session, session.begin():
        if len(action.ref) < MIN_REF:
            return "not_found"
        found = list(
            await session.scalars(
                select(RuleProposal)
                .where(
                    RuleProposal.repository_id == repository_id,
                    RuleProposal.pr_number == number,
                    RuleProposal.state.in_(REVIEWABLE),
                    RuleProposal.proposal_key.startswith(action.ref),
                )
                .with_for_update()
            )
        )
        if not found:
            return "not_found"
        if len(found) > 1:
            return "ambiguous"
        proposal = found[0]
        now = datetime.now(UTC)
        if action.kind == "confirm" and proposal.state in ("pending", "rejected"):
            proposal.state, proposal.reviewed_by, proposal.reviewed_at = "confirmed", sender, now
        elif action.kind == "uncheck" and proposal.state == "confirmed":
            proposal.state, proposal.reviewed_by, proposal.reviewed_at = "pending", None, None
        elif action.kind == "reject":
            proposal.state, proposal.reviewed_by, proposal.reviewed_at = "rejected", sender, now
        elif action.kind == "correct" and action.text:
            proposal.statement = action.text
            proposal.state, proposal.reviewed_by, proposal.reviewed_at = "corrected", sender, now
        elif action.kind == "why" and action.text:
            proposal.rationale = action.text
            proposal.rationale_source, proposal.rationale_by = "human", sender
    return None


async def _rerender(
    deps: WorkerDeps, repository: Repository, analysis: PrAnalysis, number: int, language: str
) -> bool:
    """Rebuild the comment from what was shown last time plus the reviewed proposals. No model."""
    shas = list(
        dict.fromkeys(
            await deps.github.get_pull_request_commits(
                repository.installation_id, repository.full_name, number
            )
        )
    )
    items = await load_review_items(deps, repository.id, number, shas)
    body = compose_comment(
        analysis.impact_markdown or "",
        render_proposals_section(items, language),
        render_notice(analysis.preview_notice, language),
    )
    assert analysis.comment_id is not None
    try:
        await deps.github.update_comment(
            repository.installation_id, repository.full_name, analysis.comment_id, body
        )
    except CommentNotFoundError:
        return False  # deleted by someone: the next push of the PR posts a fresh one
    return True
