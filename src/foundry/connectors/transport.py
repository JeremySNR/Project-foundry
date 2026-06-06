"""Live HTTP transports for the connectors.

The connectors take an injected ``transport`` callable so they stay testable.
These factories produce the real ones (httpx) that talk to Linear's GraphQL API
and GitHub's REST API. ``httpx`` is imported lazily so it is only required when
you actually wire a live connector; tests pass a client built on
``httpx.MockTransport`` and never hit the network.
"""

from __future__ import annotations

from typing import Any, Callable

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
GITHUB_API_BASE = "https://api.github.com"


class TransportError(RuntimeError):
    """Raised when an upstream API returns an error."""


def linear_transport(
    token: str,
    *,
    client: Any | None = None,
    url: str = LINEAR_GRAPHQL_URL,
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build the ``transport(document, variables) -> data`` Linear expects."""

    def transport(document: str, variables: dict[str, Any]) -> dict[str, Any]:
        http = client or _new_client()
        response = http.post(
            url,
            json={"query": document, "variables": variables},
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise TransportError(f"Linear GraphQL error: {payload['errors']}")
        return payload.get("data", {})

    return transport


def github_transport(
    token: str,
    *,
    client: Any | None = None,
    base: str = GITHUB_API_BASE,
) -> Callable[[str, str], Any]:
    """Build the ``transport(method, path) -> json`` GitHub connector expects."""

    def transport(method: str, path: str) -> Any:
        http = client or _new_client()
        response = http.request(
            method,
            f"{base}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return response.json()

    return transport


def _new_client() -> Any:  # pragma: no cover - only on the live path
    try:
        import httpx
    except ImportError as exc:
        raise TransportError(
            "httpx is required for live transports; install the 'http' extra"
        ) from exc
    return httpx.Client(timeout=30.0)
