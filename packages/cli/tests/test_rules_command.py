import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from rastro_cli import client
from rastro_cli.config import Config, load_config, save_config, save_token
from rastro_cli.main import app

runner = CliRunner()
REPO_ID = "11111111-1111-1111-1111-111111111111"
IMPLANTS_ID = "4f3e2d1c-0000-4000-8000-000000000001"
DISCOUNT_ID = "9a8b7c6d-0000-4000-8000-000000000002"

IMPLANTS = {
    "id": IMPLANTS_ID,
    "statement": "Implants cannot be booked on Friday afternoon",
    "rationale": "Dr. Mendes needs 48h of follow-up",
    "confidence": 0.9,
    "status": "active",
    "created_at": "2026-03-12T09:30:00Z",
    "updated_at": "2026-03-12T09:30:00Z",
    "files": ["src/agenda.ts", "src/calendar.ts"],
    "origin": {
        "commit_sha": "a1b2c3d4e5f6",
        "quote": "Dr. Mendes needs 48h of follow-up",
        "committed_at": "2026-03-12T09:30:00Z",
        "session_ref": None,
    },
    "similarity": None,
}
DISCOUNT = {
    **IMPLANTS,
    "id": DISCOUNT_ID,
    "statement": "Patients aged 65 or older get a 20% discount",
    "rationale": None,
    "files": ["src/pricing.ts"],
    "origin": None,
}
DETAIL = {
    **IMPLANTS,
    "origins": [IMPLANTS["origin"]],
    "anchors": [
        {
            "file_path": "src/agenda.ts",
            "qualified_name": "AgendaService.validate",
            "role": "primary",
            "status": "active",
            "start_line": 4,
            "end_line": 9,
        },
        {
            "file_path": "src/old.ts",
            "qualified_name": "gone",
            "role": "duplicate",
            "status": "removed",
            "start_line": 1,
            "end_line": 3,
        },
    ],
}


class Server:
    """Records requests and answers from canned payloads."""

    def __init__(self, items: list[dict[str, Any]] | None = None, search: str | None = None):
        self.items = [IMPLANTS, DISCOUNT] if items is None else items
        self.search = search
        self.requests: list[httpx.Request] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        path = request.url.path
        if path == "/v1/auth/whoami":
            return httpx.Response(200, json={"repository_id": REPO_ID, "full_name": "acme/clinic"})
        if path == f"/v1/repos/{REPO_ID}/rules":
            body = {"items": self.items, "total": len(self.items), "search": self.search}
            return httpx.Response(200, json=body)
        if path == f"/v1/repos/{REPO_ID}/rules/{IMPLANTS_ID}":
            return httpx.Response(200, json=DETAIL)
        return httpx.Response(404, json={})

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(self.requests[index].url.params)


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


def test_lists_rules_with_their_reason_files_and_origin(configured: Path, server: Server) -> None:
    result = runner.invoke(app, ["rules"])

    assert result.exit_code == 0, result.output
    assert server.requests[0].url.path == f"/v1/repos/{REPO_ID}/rules"
    assert server.params() == {"status": "active", "limit": "20", "offset": "0"}
    assert server.requests[0].headers["authorization"] == "Bearer rst_token"
    out = result.output
    assert "2 of 2 rule(s) [active]" in out
    assert "1. Implants cannot be booked on Friday afternoon" in out
    assert "Why: Dr. Mendes needs 48h of follow-up" in out
    assert "Files: src/agenda.ts, src/calendar.ts" in out
    assert "Since: commit a1b2c3d (12 Mar 2026) - id 4f3e2d1c" in out
    assert "unknown origin" in out  # a rule without a reason says so instead of inventing one


def test_file_is_relative_to_the_current_directory_and_sent_repo_relative(
    configured: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    (configured / "src").mkdir()
    monkeypatch.chdir(configured / "src")

    runner.invoke(app, ["rules", "--file", "agenda.ts"])
    relative = server.params()["file"]
    runner.invoke(app, ["rules", "-f", str(configured / "src" / "calendar.ts")])
    absolute = server.params()["file"]
    runner.invoke(app, ["rules", "--file", ".."])
    root_filter = server.params()

    assert relative == "src/agenda.ts" and absolute == "src/calendar.ts"
    assert "file" not in root_filter  # the repository root means no filter


def test_a_file_outside_the_repository_is_refused(configured: Path, server: Server) -> None:
    result = runner.invoke(app, ["rules", "--file", str(configured.parent / "elsewhere.ts")])

    assert result.exit_code == 1 and "outside this repository" in result.output
    assert server.requests == []


def test_search_is_passed_on_and_the_kind_of_search_is_reported(
    configured: Path, server: Server
) -> None:
    server.items, server.search = [{**IMPLANTS, "similarity": 0.87}], "semantic"

    result = runner.invoke(app, ["rules", "--search", "can implants be booked on fridays"])

    assert server.params()["q"] == "can implants be booked on fridays"
    assert 'for "can implants be booked on fridays" - semantic search' in result.output
    assert "(87% match)" in result.output

    server.search = "text"
    text = runner.invoke(app, ["rules", "-s", "implants"])
    assert "text search (no embedding model)" in text.output


def test_status_and_limit_are_forwarded_and_validated(configured: Path, server: Server) -> None:
    runner.invoke(app, ["rules", "--status", "all", "--limit", "5"])
    assert server.params()["status"] == "all" and server.params()["limit"] == "5"

    bad = runner.invoke(app, ["rules", "--status", "nonsense"])
    assert bad.exit_code == 2 and "must be one of" in bad.output
    assert runner.invoke(app, ["rules", "--limit", "500"]).exit_code != 0


def test_orphaned_rules_are_marked_and_truncated_lists_say_so(
    configured: Path, server: Server
) -> None:
    server.items = [{**DISCOUNT, "status": "orphaned"}]

    out = runner.invoke(app, ["rules", "--status", "orphaned"]).output

    assert "[orphaned]" in out
    assert "more; raise --limit" not in out

    class Capped(Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"items": [IMPLANTS], "total": 5, "search": None})

    client.transport = httpx.MockTransport(Capped())
    assert "4 more; raise --limit" in runner.invoke(app, ["rules"]).output


def test_json_output_is_the_server_response(configured: Path, server: Server) -> None:
    result = runner.invoke(app, ["rules", "--json"])

    assert json.loads(result.output)["total"] == 2


def test_no_results_says_what_was_searched(configured: Path, server: Server) -> None:
    server.items = []

    assert "No active rules found." in runner.invoke(app, ["rules"]).output
    assert (
        'No rules found matching "x".'
        in runner.invoke(app, ["rules", "--status", "all", "--search", "x"]).output
    )


def test_unconfigured_repository_is_pointed_to_init(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["rules"])

    assert result.exit_code == 1 and "rastro init" in result.output


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected the token"), (403, "rejected the token"), (500, "HTTP 500")],
)
def test_server_errors_become_readable_messages(
    configured: Path, server: Server, status: int, message: str
) -> None:
    server.status = status

    result = runner.invoke(app, ["rules"])

    assert result.exit_code == 1 and message in result.output


def test_an_unreachable_server_is_reported(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(client, "transport", httpx.MockTransport(down))

    result = runner.invoke(app, ["rules"])

    assert result.exit_code == 1 and "Could not reach the server" in result.output


def test_a_missing_repository_id_is_fetched_once_and_cached(
    repo: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config(repo, Config(api_url="http://rastro.test"))  # written by an older `rastro init`
    save_token(repo, "rst_token")
    monkeypatch.chdir(repo)

    assert runner.invoke(app, ["rules"]).exit_code == 0
    assert [r.url.path for r in server.requests] == [
        "/v1/auth/whoami",
        f"/v1/repos/{REPO_ID}/rules",
    ]
    config = load_config(repo)
    assert config is not None and config.repository_id == REPO_ID

    runner.invoke(app, ["rules"])
    assert [r.url.path for r in server.requests].count("/v1/auth/whoami") == 1


# --------------------------------------------------------------------------- rastro rule


def test_rule_resolves_an_id_prefix_and_shows_code_and_history(
    configured: Path, server: Server
) -> None:
    result = runner.invoke(app, ["rule", "4f3e2d1c"])

    assert result.exit_code == 0, result.output
    assert server.requests[-1].url.path == f"/v1/repos/{REPO_ID}/rules/{IMPLANTS_ID}"
    out = result.output
    assert "Implants cannot be booked on Friday afternoon" in out
    assert "why:        Dr. Mendes needs 48h of follow-up" in out
    assert "primary   src/agenda.ts:4-9  AgendaService.validate" in out
    assert "duplicate src/old.ts  gone  (removed)" in out
    assert "commit a1b2c3d  12 Mar 2026" in out
    assert '"Dr. Mendes needs 48h of follow-up"' in out


def test_rule_accepts_a_full_id_without_listing(configured: Path, server: Server) -> None:
    runner.invoke(app, ["rule", IMPLANTS_ID])

    assert [r.url.path for r in server.requests] == [f"/v1/repos/{REPO_ID}/rules/{IMPLANTS_ID}"]


def test_unknown_and_ambiguous_prefixes_are_explained(configured: Path, server: Server) -> None:
    unknown = runner.invoke(app, ["rule", "deadbeef"])
    server.items = [IMPLANTS, {**DISCOUNT, "id": "4f3e2d1c-9999-4000-8000-000000000003"}]
    ambiguous = runner.invoke(app, ["rule", "4f3e"])

    assert unknown.exit_code == 1 and "No rule has an id starting with deadbeef" in unknown.output
    assert ambiguous.exit_code == 1 and "ambiguous" in ambiguous.output


def test_rule_json_output(configured: Path, server: Server) -> None:
    out = runner.invoke(app, ["rule", IMPLANTS_ID, "--json"]).output

    assert json.loads(out)["anchors"][0]["qualified_name"] == "AgendaService.validate"
