from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from rastro_cli import client, rules_cache
from rastro_cli.config import Config, save_config, save_token
from rastro_cli.main import app

runner = CliRunner()
REPO_ID = "11111111-1111-1111-1111-111111111111"
SNAPSHOT: dict[str, Any] = {
    "head_sha": "d27aa39",
    "generated_at": "2026-10-19T16:00:00Z",
    "truncated": False,
    "rules": [
        {
            "id": "r1",
            "statement": "Implants cannot be booked on Friday afternoon",
            "rationale": None,
            "validation": "unvalidated",
            "revision": 1,
            "anchors": [
                {"file_path": "src/agenda.ts", "qualified_name": "validate", "role": "primary"}
            ],
        }
    ],
}


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.snapshot = SNAPSHOT
        self.etag = '"abc"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != f"/v1/repos/{REPO_ID}/rules/snapshot":
            return httpx.Response(404, json={})
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, headers={"ETag": self.etag})
        return httpx.Response(200, json=self.snapshot, headers={"ETag": self.etag})


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


def test_sync_stores_the_snapshot_and_then_sends_the_etag(configured: Path, server: Server) -> None:
    first = runner.invoke(app, ["sync"])
    cached = rules_cache.load(configured)

    assert first.exit_code == 0, first.output
    assert "Rules cache updated: 1 rule." in first.output
    assert cached is not None and cached["etag"] == '"abc"' and cached["head_sha"] == "d27aa39"
    assert server.requests[0].headers["authorization"] == "Bearer rst_token"
    assert "if-none-match" not in server.requests[0].headers

    second = runner.invoke(app, ["sync"])

    assert second.exit_code == 0 and "up to date" in second.output
    assert server.requests[1].headers["if-none-match"] == '"abc"'


def test_sync_replaces_a_stale_cache_and_warns_about_a_cut_list(
    configured: Path, server: Server
) -> None:
    runner.invoke(app, ["sync"])
    server.etag = '"def"'
    server.snapshot = {**SNAPSHOT, "truncated": True, "rules": []}

    result = runner.invoke(app, ["sync"])

    assert "0 rules (the server cut the list" in result.output
    assert rules_cache.etag_of(rules_cache.load(configured)) == '"def"'


def test_a_failed_sync_keeps_the_old_cache(
    configured: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["sync"])
    monkeypatch.setattr(client, "transport", httpx.MockTransport(lambda r: httpx.Response(503)))

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1
    assert rules_cache.etag_of(rules_cache.load(configured)) == '"abc"'
