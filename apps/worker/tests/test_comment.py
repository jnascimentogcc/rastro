from datetime import UTC, datetime

from rastro_worker.comment import MARKER, Finding, render_all_clear, render_comment

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
