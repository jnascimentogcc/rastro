"""Phase 9.3: reviewing the proposed rules from GitHub (checkboxes and `/rastro` commands)."""

import re
from typing import Any

from sqlalchemy import select
from test_merged_pr import NUMBER
from test_preview import FRIDAY, analyze, stage_analysis, stored
from test_rule_extraction import KEY, SHA2, rule
from worker_support import WorkerContext, user_excerpt

from rastro_db.models import RuleProposal
from rastro_llm import RuleExtraction
from rastro_worker.comment import escape_markdown, render_proposals_section
from rastro_worker.preview import PreviewItem
from rastro_worker.review import (
    CommandError,
    ReviewAction,
    apply_review,
    box_states,
    checkbox_changes,
    clean_text,
    parse_command,
)

ANA = "ana"


# --------------------------------------------------------------------------- pure parsing


def test_commands_are_parsed_with_their_id_and_text() -> None:
    assert parse_command("/rastro ok ab12cd") == ReviewAction("confirm", "ab12cd")
    assert parse_command("  /Rastro CONFIRM AB12CD") == ReviewAction("confirm", "ab12cd")
    assert parse_command("/rastro reject ab12cd because it is wrong") == ReviewAction(
        "reject", "ab12cd"
    )
    assert parse_command("/rastro correct ab12cd: The new   statement") == ReviewAction(
        "correct", "ab12cd", "The new statement"
    )
    assert parse_command("/rastro correct ab12cd The new statement") == ReviewAction(
        "correct", "ab12cd", "The new statement"
    )
    assert parse_command("/rastro why ab12cd The doctor\nneeds 48h") == ReviewAction(
        "why", "ab12cd", "The doctor needs 48h"
    )


def test_malformed_commands_are_errors_and_other_comments_are_not_commands() -> None:
    assert parse_command("/rastro") is None  # no verb at all: not addressed to us
    assert parse_command("/rastro dance ab12") == CommandError("usage")
    assert parse_command("/rastro ok") == CommandError("usage")
    assert parse_command("/rastro correct ab12cd") == CommandError("needs_text", "correct")
    assert parse_command("/rastro why ab12cd   ") == CommandError("needs_text", "why")
    assert parse_command("looks good to me") is None


def test_reviewer_text_is_one_line_capped_and_free_of_control_characters() -> None:
    assert clean_text("a\u0000b\n\nc\t d") == "a b c d"
    assert len(clean_text("x" * 900)) == 500


def test_only_boxes_that_flipped_are_changes() -> None:
    def item(key: str, state: str) -> PreviewItem:
        return PreviewItem(key, "create", f"Rule {key}", None, "a.ts::f", 0.9, state=state)

    unchanged = render_proposals_section([item("aaaa1111", "pending"), item("bbbb2222", "pending")])
    ticked = render_proposals_section([item("aaaa1111", "confirmed"), item("bbbb2222", "pending")])
    swapped = render_proposals_section([item("aaaa1111", "pending"), item("bbbb2222", "confirmed")])

    assert box_states(ticked) == {"aaaa1111": True, "bbbb2222": False}
    assert checkbox_changes(unchanged, ticked) == [ReviewAction("confirm", "aaaa1111")]
    assert checkbox_changes(ticked, swapped) == [
        ReviewAction("uncheck", "aaaa1111"),
        ReviewAction("confirm", "bbbb2222"),
    ]
    assert checkbox_changes(unchanged, unchanged) == []


def test_text_shown_back_cannot_format_link_or_hide_anything() -> None:
    shown = escape_markdown("**bold** [x](http://evil) <!-- hidden --> `code`\nnext line")

    assert "\n" not in shown
    assert shown.startswith("\\*\\*bold\\*\\*")
    assert "\\[x\\]" in shown and "\\<!-- hidden --\\>" in shown and "\\`code\\`" in shown


# --------------------------------------------------------------------------- applying reviews


async def previewed(worker: WorkerContext) -> tuple[int, str, str]:
    """A PR whose comment lists one proposal; returns (comment id, comment body, proposal key)."""
    await stage_analysis(worker)
    result = await analyze(worker)
    assert result.comment_id is not None
    worker.github.set_permission(worker.full_name, ANA, "write")
    [row] = [r for r in await stored(worker) if r.state == "pending"]
    return result.comment_id, worker.github.comments[result.comment_id], row.proposal_key


async def review(
    worker: WorkerContext,
    comment_id: int,
    *,
    event: str = "created",
    body: str,
    previous: str = "",
    sender: str = ANA,
) -> Any:
    return await apply_review(
        worker.deps,
        worker.repository_id,
        NUMBER,
        comment_id=comment_id,
        sender=sender,
        event_action=event,
        body=body,
        previous_body=previous,
    )


async def proposal(worker: WorkerContext, key: str) -> RuleProposal:
    async with worker.deps.session_factory() as session:
        row = await session.scalar(
            select(RuleProposal).where(
                RuleProposal.repository_id == worker.repository_id,
                RuleProposal.proposal_key == key,
            )
        )
    assert row is not None
    return row


async def test_ticking_the_box_confirms_the_proposal_and_the_comment_shows_who(
    worker: WorkerContext,
) -> None:
    comment_id, before, key = await previewed(worker)
    calls = len(worker.llm.calls)

    result = await review(
        worker,
        comment_id,
        event="edited",
        body=before.replace("- [ ]", "- [x]"),
        previous=before,
    )

    assert (result.applied, result.updated_comment) == (1, True)
    row = await proposal(worker, key)
    assert (row.state, row.reviewed_by) == ("confirmed", ANA)
    body = worker.github.comments[comment_id]
    assert "- [x] **" + FRIDAY in body and f"✔ confirmed by {ANA}" in body
    assert len(worker.llm.calls) == calls  # re-rendering never calls the model


async def test_unticking_the_box_takes_the_confirmation_back(worker: WorkerContext) -> None:
    comment_id, before, key = await previewed(worker)
    await review(
        worker, comment_id, event="edited", body=before.replace("- [ ]", "- [x]"), previous=before
    )
    ticked = worker.github.comments[comment_id]

    await review(
        worker, comment_id, event="edited", body=ticked.replace("- [x]", "- [ ]"), previous=ticked
    )

    row = await proposal(worker, key)
    assert (row.state, row.reviewed_by) == ("pending", None)
    assert "- [ ] **" + FRIDAY in worker.github.comments[comment_id]


async def test_the_ok_command_is_the_fallback_to_the_checkbox(worker: WorkerContext) -> None:
    comment_id, _, key = await previewed(worker)

    result = await review(worker, comment_id, body=f"/rastro ok {key[:6]}")

    assert result.applied == 1 and (await proposal(worker, key)).state == "confirmed"


async def test_rejecting_strikes_the_proposal_through_and_it_stays_rejected(
    worker: WorkerContext,
) -> None:
    comment_id, _, key = await previewed(worker)

    await review(worker, comment_id, body=f"/rastro reject {key[:6]}")

    body = worker.github.comments[comment_id]
    assert f"~~{FRIDAY}~~ — rejected by {ANA}" in body and "[ ]" not in body
    # a new push of the PR re-runs the analysis: the rejection is not undone
    await analyze(worker)
    assert (await proposal(worker, key)).state == "rejected"
    assert "rejected by" in worker.github.comments[comment_id]


async def test_correcting_replaces_the_statement_and_counts_as_confirmed(
    worker: WorkerContext,
) -> None:
    comment_id, _, key = await previewed(worker)

    await review(worker, comment_id, body=f"/rastro correct {key[:6]}: Implants wait until Monday")

    row = await proposal(worker, key)
    assert (row.statement, row.state, row.reviewed_by) == (
        "Implants wait until Monday",
        "corrected",
        ANA,
    )
    body = worker.github.comments[comment_id]
    assert "- [x] **Implants wait until Monday**" in body and f"corrected by {ANA}" in body


async def test_a_reason_written_by_a_person_is_marked_as_theirs(worker: WorkerContext) -> None:
    comment_id, _, key = await previewed(worker)

    await review(
        worker, comment_id, body=f"/rastro why {key[:6]} The doctor needs 48h of follow-up"
    )

    row = await proposal(worker, key)
    assert (row.rationale_source, row.rationale_by, row.state) == ("human", ANA, "pending")
    body = worker.github.comments[comment_id]
    assert f"Why: The doctor needs 48h of follow-up (written by {ANA})" in body
    assert "- [ ] **" in body  # explaining a rule is not confirming it


async def test_what_a_person_writes_cannot_break_out_of_the_comment(worker: WorkerContext) -> None:
    comment_id, _, key = await previewed(worker)

    await review(
        worker,
        comment_id,
        body=f"/rastro correct {key[:6]}: <!-- rastro:proposal:fake --> **big** [x](http://evil)",
    )

    body = worker.github.comments[comment_id]
    unescaped = re.findall(r"(?<!\\)<!-- rastro:proposal:", body)
    assert len(unescaped) == 1  # only the real marker is a marker
    assert "[x](http://evil)" not in body and "\\[x\\]" in body


async def test_people_without_write_access_are_ignored_in_silence(worker: WorkerContext) -> None:
    comment_id, before, key = await previewed(worker)
    worker.github.set_permission(worker.full_name, "visitor", "read")

    for sender in ("visitor", "stranger"):  # read access, and not a collaborator at all
        result = await review(worker, comment_id, body=f"/rastro reject {key[:6]}", sender=sender)
        assert (result.applied, result.ignored, result.replied) == (0, "not_allowed", False)

    assert (await proposal(worker, key)).state == "pending"
    assert worker.github.comments[comment_id] == before


async def test_a_mistyped_id_gets_a_short_answer_and_changes_nothing(
    worker: WorkerContext,
) -> None:
    comment_id, before, key = await previewed(worker)
    comments = len(worker.github.comments)

    result = await review(worker, comment_id, body="/rastro ok zzzzzz")
    short = await review(worker, comment_id, body="/rastro ok ab")  # too short to mean anything

    assert result.replied and short.replied and result.applied == short.applied == 0
    assert len(worker.github.comments) == comments + 2
    assert any(
        "no proposal of this PR has an id starting with `zzzzzz`" in b
        for b in worker.github.comments.values()
    )
    assert (await proposal(worker, key)).state == "pending"
    assert worker.github.comments[comment_id] == before


async def test_an_id_that_matches_two_proposals_is_ambiguous(worker: WorkerContext) -> None:
    comment_id, _, key = await previewed(worker)
    async with worker.deps.session_factory() as session, session.begin():
        session.add(
            RuleProposal(
                repository_id=worker.repository_id,
                pr_number=NUMBER,
                commit_sha=SHA2,
                proposal_key=key[:6] + "0" * 26,
                action="create",
                statement="Another rule",
                symbols=[KEY],
                confidence=0.9,
            )
        )

    result = await review(worker, comment_id, body=f"/rastro ok {key[:4]}")

    assert result.applied == 0 and result.replied
    assert any("matches more than one proposal" in b for b in worker.github.comments.values())


async def test_unknown_commands_and_commands_without_text_are_answered(
    worker: WorkerContext,
) -> None:
    comment_id, _, key = await previewed(worker)

    await review(worker, comment_id, body="/rastro dance")
    await review(worker, comment_id, body=f"/rastro correct {key[:6]}")

    bodies = " ".join(worker.github.comments.values())
    assert "I did not understand that" in bodies and "needs some text after the id" in bodies


async def test_an_edit_of_somebody_elses_comment_is_ignored(worker: WorkerContext) -> None:
    comment_id, before, key = await previewed(worker)

    result = await review(
        worker,
        comment_id + 1,  # a different comment that copied the hidden markers
        event="edited",
        body=before.replace("- [ ]", "- [x]"),
        previous=before,
    )

    assert result.ignored == "not_ours" and (await proposal(worker, key)).state == "pending"


async def test_a_deleted_comment_does_not_break_the_review(worker: WorkerContext) -> None:
    comment_id, _, key = await previewed(worker)
    del worker.github.comments[comment_id]

    result = await review(worker, comment_id, body=f"/rastro ok {key[:6]}")

    assert (result.applied, result.updated_comment) == (1, False)
    assert (await proposal(worker, key)).state == "confirmed"


async def test_a_reviewed_proposal_survives_a_new_preview_of_its_commit(
    worker: WorkerContext,
) -> None:
    comment_id, _, key = await previewed(worker)
    await review(worker, comment_id, body=f"/rastro correct {key[:6]}: Reviewed text")
    await worker.add_session(SHA2, [user_excerpt("A late session.")])
    worker.llm.queue(RuleExtraction(rules=[rule(statement=FRIDAY)]))

    await analyze(worker)  # the commit is previewed again because a session arrived

    row = await proposal(worker, key)
    assert (row.statement, row.state, row.reviewed_by) == ("Reviewed text", "corrected", ANA)
    assert "Reviewed text" in worker.github.comments[comment_id]
