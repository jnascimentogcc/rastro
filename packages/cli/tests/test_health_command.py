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
        metric("R4", "Confirmation outdated", 1, 4),
        {
            "id": "R6",
            "label": "Oldest rules nobody confirmed",
            "numerator": 24,
            "available": True,
            "detail": {"oldest_days": 40, "median_days": 25},
        },
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
