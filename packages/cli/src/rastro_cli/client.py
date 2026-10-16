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
