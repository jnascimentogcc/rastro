import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from rastro_cli import client
from rastro_cli.config import Config, save_config, save_token
from rastro_cli.main import app

runner = CliRunner()
REPO_ID = "11111111-1111-1111-1111-111111111111"
RULE_ID = "2a7851aa-0000-4000-8000-000000000001"


def metric(
    metric_id: str, label: str, numerator: int | None, denominator: int | None, **extra: Any
) -> dict[str, Any]:
    available = denominator not in (None, 0)
    return {
        "id": metric_id,
        "label": label,
        "numerator": numerator if available else None,
        "denominator": denominator if available else None,
        "value": numerator / denominator
        if available and numerator is not None and denominator
        else None,
        "available": available,
        "na_reason": None if available else "no active rules",
        "small_sample": bool(available and denominator is not None and denominator < 10),
        "detail": {},
        **extra,
    }


REPORT: dict[str, Any] = {
    "repository_id": REPO_ID,
    "generated_at": "2026-10-21T12:00:00Z",
    "window_days": 30,
    "head_sha": "d27aa39" + "0" * 33,
    "definitions_version": 1,
    "warnings": [
        "2 commit(s) were not analysed because of the cost budget or the model quota: the numbers below may be out of date."
    ],
    "pipeline": {
        "analysis_failed": 0,
        "skipped_budget": 2,
        "skipped_quota": 0,
        "awaiting_merge": 0,
    },
    "knowledge": [
        metric(
            "R1",
            "Rules by status",
            27,
            None,
            detail={"active": 27, "orphaned": 1, "rejected": 2, "superseded": 0},
        ),
        metric(
            "R2",
            "With a known reason",
            19,
            27,
            detail={"from_conversation": 6, "written_by_people": 13},
        ),
        metric("R3", "Confirmed by a person", 3, 27),
        metric("R3b", "...verified by GitHub", 1, 3, detail={"unverified": 2}),
        metric("R3a", "...by someone other than the PR author", 1, 2, detail={"unknown": 1}),
        metric("R4", "Confirmation outdated", 1, 4),
        {
            "id": "R6",
            "label": "Oldest rules nobody confirmed",
            "numerator": 24,
            "available": True,
            "detail": {"oldest_days": 40, "median_days": 25},
        },
    ],
    "process": [
        {"id": "P1", "label": "PRs", "numerator": 4, "available": True, "detail": {}},
        metric("P2", "Reviewed before the merge", 3, 4),
        metric("P3", "Integrated without review", 1, 4),
        {
            "id": "P4",
            "label": "Time",
            "available": False,
            "na_reason": "fewer than 3 distinct reviewers: a breakdown could point at a person",
            "detail": {},
        },
        metric(
            "P5",
            "Confirmed as written",
            5,
            8,
            detail={"confirmed": 5, "corrected": 2, "rejected": 1},
        ),
    ],
    "agents": [
        metric(
            "C1",
            "Files announced first",
            9,
            15,
            detail={
                "with_hook": {"announced": 9, "edited": 12},
                "without_hook": {"announced": 0, "edited": 3},
                "sessions_with_unknown_hook": 3,
            },
        ),
        metric("C2", "Sessions where the hook ran", 7, 11, detail={"unknown": 3}),
    ],
    "intent": [
        metric(
            "V1",
            "Integrations with no recorded intent",
            6,
            14,
            detail={
                "pull_requests": 12,
                "pull_requests_with_session": 6,
                "direct_commits_with_session": 2,
                "unattributed_commits": 40,
                "undated_units": 0,
            },
        ),
    ],
    "zones": [
        {
            "name": "src/domain",
            "symbols": 40,
            "symbols_with_rule": 12,
            "rules": 10,
            "validated": 3,
            "without_reason": 5,
        },
        {
            "name": "src/api",
            "symbols": 0,
            "symbols_with_rule": 0,
            "rules": 0,
            "validated": 0,
            "without_reason": 0,
        },
    ],
    "queue": [
        {
            "rule_id": RULE_ID,
            "statement": "Each client may have at most 3 active appointments at the same time",
            "reasons": ["no_rationale", "not_validated"],
            "callers": 7,
            "command": "rastro rule 2a7851 --confirm",
        }
    ],
    "definitions": None,
}
DEFINITIONS = {
    key: {
        "title": key,
        "formula": f"formula of {key}",
        "source": f"source of {key}",
        "not_proof": f"limit of {key}",
    }
    for key in ("R1", "R2", "R3", "R3b", "R4", "R6", "V2", "zones", "queue")
}


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.report = REPORT
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if request.url.path == f"/v1/repos/{REPO_ID}/health":
            explained = request.url.params.get("explain") == "true"
            return httpx.Response(
                200, json={**self.report, "definitions": DEFINITIONS if explained else None}
            )
        return httpx.Response(404, json={})

    def params(self) -> list[tuple[str, str]]:
        return list(self.requests[-1].url.params.multi_items())


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server()
    monkeypatch.setattr(client, "transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def configured(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    save_config(repo, Config(api_url="http://rastro.test", repository_id=REPO_ID))
    save_token(repo, "rst_token")
    monkeypatch.chdir(repo)
    return repo


def test_the_report_shows_numerators_denominators_and_what_to_do_next(
    configured: Path, server: Server
) -> None:
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "Rastro health  (rules of d27aa39; pipeline problems of the last 30 days)" in out
    assert "! 2 commit(s) were not analysed" in out
    assert "Active rules                    27   (1 orphaned, 2 rejected)" in out
    assert (
        "With a known reason             19 of 27 (70%)   6 from conversations, 13 written by people"
        in out
    )
    assert "Confirmed by a person           3 of 27 (11%)" in out
    assert "...verified by GitHub         1 of 3 (33%)" in out
    assert "Oldest unconfirmed rule         40 days (median 25), 24 rule(s) waiting" in out
    assert (
        "1. 2a7851  Each client may have at most 3 active appointments at the..." in out
    )  # shortened
    assert "no reason recorded; not confirmed by a person; 7 caller(s)" in out
    assert "rastro rule 2a7851 --confirm" in out
    assert "Zones" not in out  # only with --zones


def test_it_asks_the_server_for_what_was_requested(configured: Path, server: Server) -> None:
    runner.invoke(
        app,
        [
            "health",
            "--window",
            "4w",
            "--zones",
            "--queue",
            "9",
            "--exclude",
            "src/legacy/*",
            "--exclude",
            "scripts/*",
        ],
    )

    assert server.params() == [
        ("window", "28"),
        ("zones", "true"),
        ("queue", "9"),
        ("explain", "false"),
        ("exclude", "src/legacy/*"),
        ("exclude", "scripts/*"),
    ]
    assert server.requests[-1].headers["authorization"] == "Bearer rst_token"


def test_zones_are_listed_with_their_shares(configured: Path, server: Server) -> None:
    out = runner.invoke(app, ["health", "--zones"]).output

    assert "Zones (directories; tests, dependencies and generated code left out)" in out
    assert (
        "src/domain" in out
        and "symbols   40, with a rule  12 (30%), rules  10, confirmed   3, no reason   5" in out
    )
    assert "src/api" in out and "(n/a)" in out  # a zone without symbols has no share


def test_a_figure_that_cannot_be_known_says_why_instead_of_showing_zero(
    configured: Path, server: Server
) -> None:
    server.report = {
        **REPORT,
        "warnings": [],
        "knowledge": [
            metric(
                "R1",
                "Rules",
                0,
                None,
                detail={"active": 0, "orphaned": 0, "rejected": 0, "superseded": 0},
            ),
            metric("R2", "x", 0, 0),
            metric("R3", "x", 0, 0),
            metric("R3b", "x", 0, 0),
            metric("R4", "x", 0, 0),
            {
                "id": "R6",
                "label": "x",
                "available": False,
                "na_reason": "every active rule is confirmed",
                "detail": {},
            },
        ],
        "queue": [],
        "process": [],
        "intent": [],
        "agents": [],
    }

    out = runner.invoke(app, ["health"]).output

    assert "With a known reason             n/a (no active rules)" in out
    assert "Oldest unconfirmed rule         n/a (every active rule is confirmed)" in out
    assert "0%" not in out
    assert "Review queue" not in out  # nothing to show and no explanation asked


def test_a_small_sample_is_flagged(configured: Path, server: Server) -> None:
    out = runner.invoke(app, ["health"]).output

    assert (
        "Confirmed by a person           3 of 27 (11%)" in out
        and "small sample" not in out.split("Confirmed by a person")[1].splitlines()[0]
    )
    assert "1 of 3 (33%) - small sample" in out


def test_explain_adds_the_definition_source_and_limit_of_each_figure(
    configured: Path, server: Server
) -> None:
    out = runner.invoke(app, ["health", "--explain", "--zones"]).output

    for key in ("R1", "R2", "R3", "R3b", "R4", "R6", "V2", "zones", "queue"):
        assert f"how: formula of {key}" in out
        assert f"from: source of {key}" in out
        assert f"does not prove: limit of {key}" in out


def test_json_prints_the_server_answer_untouched(configured: Path, server: Server) -> None:
    result = runner.invoke(app, ["health", "--json"])

    assert json.loads(result.output) == {**REPORT, "definitions": None}


@pytest.mark.parametrize("window", ["abc", "6", "400d", "0"])
def test_a_bad_window_is_refused_before_asking_the_server(
    configured: Path, server: Server, window: str
) -> None:
    result = runner.invoke(app, ["health", "--window", window])

    assert result.exit_code != 0
    assert server.requests == []


def test_server_problems_get_a_clear_message(configured: Path, server: Server) -> None:
    server.status = 401

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1 and "rejected the token" in result.output


def test_the_process_section_shows_counts_and_withholds_what_could_name_a_reviewer(
    configured: Path, server: Server
) -> None:
    out = runner.invoke(app, ["health"]).output

    assert "Process (last 30 days)" in out
    assert "Pull requests with proposals    4" in out
    assert "Reviewed before the merge       3 of 4 (75%) - small sample" in out
    assert "Integrated without review       1 of 4 (25%) - small sample" in out
    assert "Time to first review            n/a (fewer than 3 distinct reviewers" in out
    assert "5 of 8 (62%) - small sample confirmed as written, 2 corrected, 1 rejected" in out


def test_the_provenance_section_says_what_it_leaves_out_and_why(
    configured: Path, server: Server
) -> None:
    out = runner.invoke(app, ["health"]).output

    assert "Code without recorded intent (last 30 days)" in out
    assert "Integrations with no session    6 of 14 (43%)" in out
    assert "40 commit(s) with no session and no pull request are left out" in out
    assert "cannot be told apart from older merge commits" in out


def test_the_confirmations_by_someone_else_are_listed_under_the_confirmed_ones(
    configured: Path, server: Server
) -> None:
    out = runner.invoke(app, ["health"]).output

    assert "...by someone other than the PR author  1 of 2 (50%) - small sample" in out


def test_a_report_from_an_older_server_without_the_new_sections_still_prints(
    configured: Path, server: Server
) -> None:
    older = {key: value for key, value in REPORT.items() if key not in ("process", "intent")}
    older["knowledge"] = [m for m in REPORT["knowledge"] if m["id"] != "R3a"]
    server.report = older

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.output
    assert "Process (" not in result.output and "Code without recorded intent" not in result.output


def test_explain_covers_the_new_figures(
    configured: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = ("R3a", "P1", "P2", "P3", "P4", "P5", "V1")
    for key in keys:
        definition = {
            "title": key,
            "formula": f"formula of {key}",
            "source": f"source of {key}",
            "not_proof": f"limit of {key}",
        }
        monkeypatch.setitem(DEFINITIONS, key, definition)

    out = runner.invoke(app, ["health", "--explain"]).output

    for key in keys:
        assert f"how: formula of {key}" in out


def test_the_agents_section_separates_having_the_hook_from_using_it(
    configured: Path, server: Server
) -> None:
    out = runner.invoke(app, ["health"]).output

    assert "Agents (last 30 days)" in out
    assert "Files with rules announced first  9 of 15 (60%)" in out
    assert "with the hook 9 of 12, without it 0 of 3" in out
    assert "Sessions where the hook ran       7 of 11 (64%)   (3 session(s) did not say)" in out


def test_explain_covers_the_agent_figures(
    configured: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("C1", "C2"):
        monkeypatch.setitem(
            DEFINITIONS,
            key,
            {
                "title": key,
                "formula": f"formula of {key}",
                "source": f"source of {key}",
                "not_proof": f"limit of {key}",
            },
        )

    out = runner.invoke(app, ["health", "--explain"]).output

    assert "how: formula of C1" in out and "does not prove: limit of C2" in out


def test_r5_is_shown_after_the_outdated_confirmations_and_the_queue_explains_it(
    configured: Path, server: Server
) -> None:
    knowledge = [
        *REPORT["knowledge"],
        metric("R5", "Code changed after confirming", 2, 5, detail={"unknown": 2}),
    ]
    queue = [
        {
            "rule_id": RULE_ID,
            "statement": "Implants cannot be booked on Friday afternoon",
            "reasons": ["validation_outdated_by_code"],
            "callers": 0,
            "command": "rastro rule 2a7851 --confirm",
        }
    ]
    server.report = {**REPORT, "knowledge": knowledge, "queue": queue}

    out = runner.invoke(app, ["health"]).output

    assert "Code changed after confirming   2 of 5 (40%) - small sample" in out
    assert "the code changed after it was confirmed" in out
