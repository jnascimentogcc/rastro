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


def tree_json(sha: str, entries: list[tuple[str, str, str]], truncated: bool = False) -> object:
    return {
        "sha": sha,
        "url": "u",
        "truncated": truncated,
        "tree": [
            {
                "path": path,
                "mode": "040000" if kind == "tree" else "100644",
                "type": kind,
                "sha": entry_sha,
                "size": 1,
                "url": "u",
            }
            for path, kind, entry_sha in entries
        ],
    }


async def test_truncated_tree_is_walked_directory_by_directory(private_key_pem: str) -> None:
    requests: list[tuple[str, bool]] = []
    trees = {
        "root": [("README.md", "blob", "b0"), ("src", "tree", "t-src")],
        "t-src": [("a.ts", "blob", "b1"), ("deep", "tree", "t-deep")],
        "t-deep": [("b.ts", "blob", "b2")],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        sha = request.url.path.rsplit("/", 1)[1]
        recursive = "recursive" in request.url.params
        requests.append((sha, recursive))
        if recursive:  # GitHub gave up: only part of the files, flagged as truncated
            return httpx.Response(200, json=tree_json(sha, [("README.md", "blob", "b0")], True))
        return httpx.Response(200, json=tree_json(sha, trees[sha]))

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    files = await client.list_files(100, "acme/big", "root")

    assert {(f.path, f.blob_sha) for f in files} == {
        ("README.md", "b0"),
        ("src/a.ts", "b1"),
        ("src/deep/b.ts", "b2"),
    }
    assert requests[0] == ("root", True)  # tried the recursive call first
    assert {sha for sha, recursive in requests if not recursive} == {"root", "t-src", "t-deep"}


def test_private_key_accepts_escaped_newlines_and_files(tmp_path, private_key_pem: str) -> None:  # type: ignore[no-untyped-def]
    escaped = GitHubSettings(github_app_private_key=SecretStr(private_key_pem.replace("\n", "\\n")))
    path = tmp_path / "key.pem"
    path.write_text(private_key_pem)
    from_file = GitHubSettings(github_app_private_key_path=path)

    assert escaped.private_key() == private_key_pem
    assert from_file.private_key() == private_key_pem


def diff_entry(name: str, status: str = "modified", **extra: object) -> dict[str, object]:
    return {
        "sha": f"sha-{name}",
        "filename": name,
        "status": status,
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "blob_url": "https://github.com/o/r/blob/x",
        "raw_url": "https://github.com/o/r/raw/x",
        "contents_url": "https://api.github.com/repos/o/r/contents/x",
        **extra,
    }


async def test_pull_request_files_are_paginated_and_mapped(private_key_pem: str) -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        assert request.url.path == "/repos/acme/clinic/pulls/5/files"
        pages.append(request.url.params["page"])
        if request.url.params["page"] == "1":
            patch = "@@ -1 +1 @@\n-a\n+b"
            batch = [diff_entry(f"src/f{i}.ts", patch=patch) for i in range(100)]
        else:
            batch = [
                diff_entry("src/new.ts", "added"),
                diff_entry("src/gone.ts", "removed"),
                diff_entry("src/moved.ts", "renamed", previous_filename="src/old.ts"),
            ]
        return httpx.Response(200, json=batch)

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    files = await client.get_pull_request_files(111, "acme/clinic", 5)

    assert pages == ["1", "2"] and len(files) == 103
    by_path = {f.path: f for f in files}
    assert (
        by_path["src/f0.ts"].patch is not None and by_path["src/f0.ts"].blob_sha == "sha-src/f0.ts"
    )
    assert by_path["src/new.ts"].status == "added" and by_path["src/new.ts"].patch is None
    assert by_path["src/gone.ts"].status == "removed" and by_path["src/gone.ts"].blob_sha is None
    assert by_path["src/moved.ts"].previous_path == "src/old.ts"


def comment_json(comment_id: int, body: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "node_id": "n",
        "url": "https://api.github.com/x",
        "html_url": "https://github.com/x",
        "body": body,
        "user": None,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T10:00:00Z",
        "issue_url": "https://api.github.com/x",
        "author_association": "NONE",
    }


async def test_comments_are_created_on_the_issue_endpoint_and_updated_in_place(
    private_key_pem: str,
) -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        body = httpx.Request("x", "http://x", content=request.content).content.decode()
        seen.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(201, json=comment_json(42, "hello"))
        return httpx.Response(200, json=comment_json(42, "edited"))

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    comment_id = await client.create_comment(112, "acme/clinic", 5, "hello")
    await client.update_comment(112, "acme/clinic", comment_id, "edited")

    assert comment_id == 42
    assert seen[0][:2] == ("POST", "/repos/acme/clinic/issues/5/comments")
    assert seen[1][:2] == ("PATCH", "/repos/acme/clinic/issues/comments/42")
    assert "edited" in seen[1][2]


async def test_updating_a_deleted_comment_raises_a_specific_error(private_key_pem: str) -> None:
    from rastro_github import CommentNotFoundError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        return httpx.Response(404, json={"message": "Not Found"})

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    with pytest.raises(CommentNotFoundError):
        await client.update_comment(113, "acme/clinic", 42, "x")


async def test_pull_request_commits_are_paginated_oldest_first(private_key_pem: str) -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        assert request.url.path == "/repos/acme/clinic/pulls/5/commits"
        pages.append(request.url.params["page"])
        count = 100 if request.url.params["page"] == "1" else 2
        offset = 0 if request.url.params["page"] == "1" else 100
        return httpx.Response(200, json=[commit_json(f"{offset + i:040x}") for i in range(count)])

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    shas = await client.get_pull_request_commits(111, "acme/clinic", 5)

    assert pages == ["1", "2"] and len(shas) == 102
    assert shas[0] == f"{0:040x}" and shas[-1] == f"{101:040x}"


def commit_json(sha: str) -> dict[str, object]:
    person = {"name": "Dev", "email": "dev@example.com", "date": "2026-10-01T10:00:00Z"}
    return {
        "sha": sha,
        "node_id": "n",
        "url": "https://api.github.com/x",
        "html_url": "https://github.com/x",
        "comments_url": "https://api.github.com/x",
        "commit": {
            "url": "https://api.github.com/x",
            "author": person,
            "committer": person,
            "message": "m",
            "comment_count": 0,
            "tree": {"sha": "t", "url": "https://api.github.com/x"},
        },
        "author": None,
        "committer": None,
        "parents": [],
    }


def permission_json(permission: str) -> dict[str, object]:
    return {"permission": permission, "role_name": permission, "user": None}


async def test_the_collaborator_permission_is_read_from_the_permission_endpoint(
    private_key_pem: str,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        seen.append(request.url.path)
        return httpx.Response(200, json=permission_json("maintain"))

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    assert await client.get_collaborator_permission(120, "acme/clinic", "ana") == "maintain"
    assert seen == ["/repos/acme/clinic/collaborators/ana/permission"]


async def test_somebody_who_is_not_a_collaborator_has_no_permission(private_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        return httpx.Response(404, json={"message": "Not Found"})

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    assert await client.get_collaborator_permission(121, "acme/clinic", "stranger") == "none"


async def test_other_errors_while_reading_the_permission_are_not_swallowed(
    private_key_pem: str,
) -> None:
    from githubkit.exception import RequestFailed

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        return httpx.Response(500, json={"message": "boom"})

    client = make_client(private_key_pem, httpx.MockTransport(handler))

    with pytest.raises(RequestFailed):
        await client.get_collaborator_permission(122, "acme/clinic", "ana")
