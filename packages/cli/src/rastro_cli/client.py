from typing import Any

import httpx

REQUEST_TIMEOUT_SECONDS = 15.0

# Tests inject an httpx transport here (e.g. httpx.MockTransport).
transport: httpx.BaseTransport | None = None


def make_client(token: str, api_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=api_url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "rastro-cli"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        transport=transport,
    )


def whoami(token: str, api_url: str) -> dict[str, Any]:
    with make_client(token, api_url) as client:
        response = client.get("/v1/auth/whoami")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def list_rules(
    token: str,
    api_url: str,
    repository_id: str,
    *,
    file: str | None = None,
    search: str | None = None,
    status: str = "active",
    validated: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"status": status, "limit": limit, "offset": offset}
    if validated is not None:
        params["validated"] = str(validated).lower()
    if file:
        params["file"] = file
    if search:
        params["q"] = search
    with make_client(token, api_url) as client:
        response = client.get(f"/v1/repos/{repository_id}/rules", params=params)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def get_rules_snapshot(
    token: str, api_url: str, repository_id: str, etag: str | None
) -> tuple[dict[str, Any], str] | None:
    """The active-rules snapshot and its ETag, or None when `etag` is still current (304)."""
    headers = {"If-None-Match": etag} if etag else {}
    with make_client(token, api_url) as client:
        response = client.get(f"/v1/repos/{repository_id}/rules/snapshot", headers=headers)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        snapshot: dict[str, Any] = response.json()
        return snapshot, response.headers.get("etag", "")


def get_health(
    token: str,
    api_url: str,
    repository_id: str,
    *,
    window: int,
    exclude: list[str],
    zones: bool,
    queue: int,
    explain: bool,
) -> dict[str, Any]:
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("window", window),
        ("zones", str(zones).lower()),
        ("queue", queue),
        ("explain", str(explain).lower()),
        *[("exclude", pattern) for pattern in exclude],
    ]
    with make_client(token, api_url) as client:
        response = client.get(f"/v1/repos/{repository_id}/health", params=params)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def get_rule(token: str, api_url: str, repository_id: str, rule_id: str) -> dict[str, Any]:
    with make_client(token, api_url) as client:
        response = client.get(f"/v1/repos/{repository_id}/rules/{rule_id}")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def review_rule(
    token: str, api_url: str, repository_id: str, rule_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    with make_client(token, api_url) as client:
        response = client.post(f"/v1/repos/{repository_id}/rules/{rule_id}/reviews", json=body)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
