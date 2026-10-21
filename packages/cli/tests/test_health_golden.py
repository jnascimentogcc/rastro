"""Reference reports (golden files) to catch accidental format changes.

Regenerate on purpose with `RASTRO_UPDATE_GOLDEN=1 uv run pytest packages/cli/tests/test_health_golden.py`.
"""

import os
from pathlib import Path

from test_health_command import REPORT

from rastro_cli.render_health import render_health
from rastro_cli.render_health_markdown import render_markdown

GOLDEN = Path(__file__).parent / "golden"


def check(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("RASTRO_UPDATE_GOLDEN"):
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_text_report_matches_the_reference() -> None:
    check("health.txt", render_health(REPORT, zones=True, explain=False))


def test_markdown_report_matches_the_reference() -> None:
    check("health.md", render_markdown(REPORT, zones=True))


def test_markdown_keeps_a_rule_text_inside_its_table_cell() -> None:
    queue = [{**REPORT["queue"][0], "statement": "a | b\nsecond line"}]

    out = render_markdown({**REPORT, "queue": queue}, zones=False)

    assert "| a \| b second line |" in out


def test_markdown_does_not_show_zones_unless_asked() -> None:
    assert "## Zones" not in render_markdown(REPORT, zones=False)


def test_markdown_says_when_there_is_no_default_branch_commit() -> None:
    assert "No default-branch commit is known yet." in render_markdown(
        {**REPORT, "head_sha": None}, zones=False
    )
