import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cli_support import git
from test_rules_command import DETAIL, DISCOUNT, IMPLANTS, IMPLANTS_ID, REPO_ID
from typer.testing import CliRunner

from rastro_cli import client
from rastro_cli.config import Config, save_config, save_token
from rastro_cli.main import app

runner = CliRunner()
ANA = "ana@clinic.example"
REVIEWED = {
    **DETAIL,
    "validation": "validated",
    "validated_by": ANA,
    "validated_at": "2026-10-19T10:00:00Z",
    "reviews": [
        {
            "verdict": "confirmed",
            "rule_revision": 1,
            "reviewer": ANA,
            "reviewer_source": "cli",
            "statement_after": None,
            "rationale_after": None,
            "note": "checked with Dr. Mendes",
            "created_at": "2026-10-19T10:00:00Z",
        }
    ],
}


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.review_status = 200
        self.detail: dict[str, Any] = REVIEWED

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/v1/repos/{REPO_ID}/rules":
            items = [IMPLANTS, DISCOUNT]
            return httpx.Response(200, json={"items": items, "total": 2, "search": None})
        if path == f"/v1/repos/{REPO_ID}/rules/{IMPLANTS_ID}/reviews":
            if self.review_status != 200:
                return httpx.Response(self.review_status, json={"detail": "Rule was rejected"})
            return httpx.Response(200, json=self.detail)
        if path == f"/v1/repos/{REPO_ID}/rules/{IMPLANTS_ID}":
            return httpx.Response(200, json=self.detail)
        return httpx.Response(404, json={})

    def posted(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server()
    monkeypatch.setattr(client, "transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def configured(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    save_config(repo, Config(api_url="http://rastro.test", repository_id=REPO_ID))
    save_token(repo, "rst_token")
    git(repo, "config", "user.email", ANA)
    monkeypatch.chdir(repo)
    return repo


def test_the_list_can_be_narrowed_to_pending_or_rejected_rules(
    configured: Path, server: Server
) -> None:
    runner.invoke(app, ["rules", "--unvalidated"])
    pending = dict(server.requests[-1].url.params)
    runner.invoke(app, ["rules", "--rejected"])
    rejected = dict(server.requests[-1].url.params)

    assert pending["validated"] == "false" and pending["status"] == "active"
    assert rejected["status"] == "rejected" and "validated" not in rejected


def test_confirm_sends_the_git_email_and_says_it_is_not_verified(
    configured: Path, server: Server
) -> None:
    result = runner.invoke(app, ["rule", "4f3e", "--confirm", "--note", "checked with Dr. Mendes"])

    assert result.exit_code == 0, result.output
    assert server.posted() == [
        {"reviewer": ANA, "note": "checked with Dr. Mendes", "verdict": "confirmed"}
    ]
    assert f"Recorded: confirmed by {ANA} (not verified" in result.output
    assert f"review:     validated by {ANA} on 19 Oct 2026" in result.output
    assert f"confirmed by {ANA} (not verified)" in result.output
    assert '"checked with Dr. Mendes"' in result.output


def test_correct_with_a_reason_and_a_reason_alone(configured: Path, server: Server) -> None:
    runner.invoke(app, ["rule", IMPLANTS_ID, "--correct", "New text", "--why", "Because"])
    runner.invoke(app, ["rule", IMPLANTS_ID, "--why", "Only the reason"])

    first, second = server.posted()
    assert (first["verdict"], first["statement"], first["rationale"]) == (
        "corrected",
        "New text",
        "Because",
    )
    assert (second["verdict"], second["rationale"]) == ("rationale_added", "Only the reason")
    assert "statement" not in second


def test_reject(configured: Path, server: Server) -> None:
    result = runner.invoke(app, ["rule", IMPLANTS_ID, "--reject"])

    assert result.exit_code == 0 and server.posted()[0]["verdict"] == "rejected"


def test_conflicting_actions_are_refused_before_calling_the_server(
    configured: Path, server: Server
) -> None:
    both = runner.invoke(app, ["rule", IMPLANTS_ID, "--confirm", "--reject"])
    mixed = runner.invoke(app, ["rule", IMPLANTS_ID, "--confirm", "--why", "x"])
    orphan_note = runner.invoke(app, ["rule", IMPLANTS_ID, "--note", "x"])

    assert (both.exit_code, mixed.exit_code, orphan_note.exit_code) == (2, 2, 2)
    assert server.requests == []


def test_a_review_needs_a_git_identity(configured: Path, server: Server) -> None:
    git(configured, "config", "user.email", "")

    result = runner.invoke(app, ["rule", IMPLANTS_ID, "--confirm"])

    assert result.exit_code == 1 and "git config user.email" in result.output
    assert server.posted() == []


def test_a_refusal_from_the_server_is_explained(configured: Path, server: Server) -> None:
    server.review_status = 409

    result = runner.invoke(app, ["rule", IMPLANTS_ID, "--confirm"])

    assert result.exit_code == 1
    assert "Rule was rejected" in result.output


def test_a_rule_that_was_never_reviewed_says_so(configured: Path, server: Server) -> None:
    server.detail = {**DETAIL, "validation": "unvalidated", "reviews": []}

    result = runner.invoke(app, ["rule", IMPLANTS_ID])

    assert "review:     not validated" in result.output
    assert "reviews:" not in result.output
