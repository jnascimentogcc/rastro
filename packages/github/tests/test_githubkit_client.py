import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from rastro_github import GithubKitClient, GitHubSettings


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def make_client(private_key_pem: str, handler: httpx.MockTransport) -> GithubKitClient:
    settings = GitHubSettings(
        github_app_id=12345, github_app_private_key=SecretStr(private_key_pem)
    )
    return GithubKitClient(settings, async_transport=handler)


def token_response() -> httpx.Response:
    expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return httpx.Response(
        201, json={"token": "ghs_installation", "expires_at": expires, "permissions": {}}
    )


async def test_authenticates_as_installation_and_reads_a_blob(private_key_pem: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/app/installations/77/access_tokens":
            assert request.headers["authorization"].startswith("Bearer ")  # app JWT
            return token_response()
        assert request.headers["authorization"] == "token ghs_installation"
        assert request.url.path == "/repos/acme/clinic/git/blobs/abc123"
        return httpx.Response(
            200,
            json={
                "sha": "abc123",
                "node_id": "n",
                "size": 5,
                "url": "https://api.github.com/x",
                "content": base64.b64encode(b"hello").decode(),
                "encoding": "base64",
            },
        )

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    assert await client.get_blob(77, "acme/clinic", "abc123") == b"hello"
    assert [r.url.path for r in seen][0] == "/app/installations/77/access_tokens"


async def test_installation_token_is_reused_between_calls(private_key_pem: str) -> None:
    exchanges = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges
        if request.url.path.endswith("/access_tokens"):
            exchanges += 1
            return token_response()
        return httpx.Response(
            200,
            json={
                "sha": "s",
                "node_id": "n",
                "size": 1,
                "url": "u",
                "content": base64.b64encode(b"x").decode(),
                "encoding": "base64",
            },
        )

    client = make_client(private_key_pem, httpx.MockTransport(handler))
    await client.get_blob(88, "acme/clinic", "a")
    await client.get_blob(88, "acme/clinic", "b")

    assert exchanges == 1


async def test_lists_only_blobs_of_the_tree(private_key_pem: str) -> None:  # installation 99
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        assert request.url.path == "/repos/acme/clinic/git/trees/deadbeef"
        assert request.url.params["recursive"] == "1"
        return httpx.Response(
            200,
            json={
                "sha": "deadbeef",
                "url": "u",
                "truncated": False,
                "tree": [
                    {"path": "src", "mode": "040000", "type": "tree", "sha": "t1", "url": "u"},
                    {
                        "path": "src/a.ts",
                        "mode": "100644",
                        "type": "blob",
                        "sha": "b1",
                        "size": 42,
                        "url": "u",
                    },
                ],
            },
        )

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    files = await client.list_files(99, "acme/clinic", "deadbeef")

    assert [(f.path, f.blob_sha, f.size) for f in files] == [("src/a.ts", "b1", 42)]


def test_private_key_accepts_escaped_newlines_and_files(tmp_path, private_key_pem: str) -> None:  # type: ignore[no-untyped-def]
    escaped = GitHubSettings(github_app_private_key=SecretStr(private_key_pem.replace("\n", "\\n")))
    path = tmp_path / "key.pem"
    path.write_text(private_key_pem)
    from_file = GitHubSettings(github_app_private_key_path=path)

    assert escaped.private_key() == private_key_pem
    assert from_file.private_key() == private_key_pem
