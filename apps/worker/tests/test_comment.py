from datetime import UTC, datetime

from rastro_worker.comment import MARKER, Finding, render_all_clear, render_comment
from rastro_worker.preview import PreviewItem

FINDING = Finding(
    rule_id="r1",
    statement="Implants cannot be booked on Friday afternoon",
    rationale="Dr. Mendes needs 48h of follow-up",
    risk="changes",
    confidence=0.9,
    explanation="This change enables Saturday bookings, which changes the conditions of the rule.",
    unchanged_duplicates=["src/calendar.ts"],
    origin_commit="a1b2c3d4e5f6",
    origin_date=datetime(2026, 3, 12, tzinfo=UTC),
)


def test_comment_follows_the_spec_layout() -> None:
    body = render_comment([FINDING])

    assert body.startswith(MARKER + "\n### Rastro — rules affected by this PR")
    assert "**Implants cannot be booked on Friday afternoon**" in body
    assert "Why: Dr. Mendes needs 48h of follow-up" in body
    assert "Risk: This change enables Saturday bookings" in body
    assert "Warning: the same logic exists in `src/calendar.ts` and was not changed." in body
    assert "Origin: request of 03/12, commit a1b2c3d." in body


def test_portuguese_comment_uses_the_spec_labels_and_date_order() -> None:
    body = render_comment([FINDING], "pt")

    assert "### Rastro — regras afetadas por este PR" in body
    assert "Porquê: Dr. Mendes needs 48h of follow-up" in body
    assert "Atenção: a mesma lógica existe em `src/calendar.ts` e não foi alterada." in body
    assert "Origem: pedido de 12/03, commit a1b2c3d." in body


def test_missing_details_are_handled() -> None:
    bare = Finding("r2", "Rule", None, "breaks", 0.8, "It breaks.")

    body = render_comment([bare])

    assert "Why: origin unknown" in body
    assert "Warning" not in body and "Origin" not in body
    undated = Finding("r3", "Rule", None, "breaks", 0.8, "x", origin_commit="deadbeefcafe")
    assert "Origin: commit deadbee." in render_comment([undated])


def test_findings_are_separate_blocks_in_one_comment() -> None:
    body = render_comment([FINDING, Finding("r2", "Second rule", None, "breaks", 0.8, "Boom.")])

    assert body.count(MARKER) == 1 and "**Second rule**" in body


def test_all_clear_comment_keeps_the_marker_so_it_stays_the_same_comment() -> None:
    body = render_all_clear("abcdef1234567")

    assert body.startswith(MARKER) and "no rules affected" in body and "`abcdef1`" in body
    assert "nenhuma regra" in render_all_clear("abcdef1234567", "pt")


def item(n: int = 1, **overrides: object) -> PreviewItem:
    values: dict[str, object] = {
        "key": f"k{n}",
        "action": "create",
        "statement": f"Rule number {n}",
        "rationale": "The doctor needs 48h of follow-up",
        "symbol": "src/agenda.ts::AgendaService.validate",
        "confidence": 0.9,
    }
    return PreviewItem(**(values | overrides))  # type: ignore[arg-type]


def test_proposals_are_listed_without_actions_and_say_nothing_is_saved_yet() -> None:
    body = render_comment([], proposals=[item(1)])

    assert body.startswith(MARKER + "\n### Rastro — rules this PR proposes")
    assert "Nothing is saved until the PR is merged." in body
    assert "- **Rule number 1**" in body
    assert "  Why: The doctor needs 48h of follow-up" in body
    assert "  New rule · `src/agenda.ts::AgendaService.validate`" in body
    assert "[ ]" not in body  # no checkbox: there is nothing to act on yet
    assert "rules affected" not in body  # no impact section without findings


def test_a_missing_reason_is_said_not_invented() -> None:
    body = render_comment([], proposals=[item(1, rationale=None)])

    assert "Why: origin unknown (the conversation did not say why)" in body


def test_only_five_are_visible_and_the_rest_are_collapsed() -> None:
    body = render_comment([], proposals=[item(n) for n in range(1, 8)])

    visible, collapsed = body.split("<details>")
    assert all(f"Rule number {n}**" in visible for n in range(1, 6))
    assert "Rule number 6" in collapsed and "Rule number 7" in collapsed
    assert "<summary>2 more</summary>" in collapsed and collapsed.rstrip().endswith("</details>")


def test_an_update_names_the_rule_it_changes_and_who_confirmed_it() -> None:
    changed = item(
        1,
        action="update",
        statement="Implants cannot be booked on Friday or Saturday",
        replaces="Implants cannot be booked on Friday",
        validated_by="@ana",
        validated_at=datetime(2026, 10, 12, tzinfo=UTC),
    )

    body = render_comment([], proposals=[changed])

    assert "  Changes an existing rule · `src/agenda.ts::AgendaService.validate`" in body
    assert "  Currently: Implants cannot be booked on Friday" in body
    assert "⚠ Changes a rule validated by @ana on 10/12" in body


def test_proposals_are_written_in_portuguese_when_asked() -> None:
    changed = item(1, action="update", replaces="Antes", validated_by="@ana")

    body = render_comment([], "pt", proposals=[item(2), changed])

    assert "### Rastro — regras que este PR propõe" in body
    assert "Nada é guardado até o PR ser integrado." in body
    assert "Nova regra" in body and "Altera uma regra existente" in body
    assert "Atualmente: Antes" in body and "Altera uma regra validada por @ana" in body


def test_impact_warnings_and_proposals_share_one_comment() -> None:
    body = render_comment([FINDING], proposals=[item(1)])

    assert body.count(MARKER) == 1
    assert body.index("rules affected by this PR") < body.index("rules this PR proposes")


def test_the_preview_says_when_it_could_not_run() -> None:
    budget = render_comment([], unavailable="budget")
    quota = render_comment([], "pt", unavailable="quota")

    assert "Rule preview unavailable: the monthly cost limit was reached." in budget
    assert "Pré-visualização de regras indisponível: a conta do modelo ficou sem crédito." in quota


def test_an_earlier_warning_that_went_away_is_cleared_next_to_new_proposals() -> None:
    body = render_comment([], proposals=[item(1)], all_clear_sha="abcdef1234567")

    assert "no rules affected by this PR" in body and "`abcdef1`" in body
    assert "rules this PR proposes" in body


def test_a_comment_with_nothing_left_to_show_is_neutral_about_it() -> None:
    assert "none is proposed" in render_all_clear("abcdef1234567", flagged=False)
    assert "flagged earlier" in render_all_clear("abcdef1234567")
