"""Live HTTP transports for the connectors.

The connectors take an injected ``transport`` callable so they stay testable.
These factories produce the real ones (httpx) that talk to Linear's GraphQL API
and GitHub's REST API. ``httpx`` is imported lazily so it is only required when
you actually wire a live connector; tests pass a client built on
``httpx.MockTransport`` and never hit the network.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
GITHUB_API_BASE = "https://api.github.com"

_log = logging.getLogger(__name__)

# Statuses that are safe to retry (transient server errors / rate limits).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds


class TransportError(RuntimeError):
    """Raised when an upstream API returns an error."""


def _retry_sleep(attempt: int, retry_after: float | None) -> None:
    delay = retry_after if retry_after is not None else _BACKOFF_BASE ** attempt
    _log.warning("upstream request failed; retrying in %.1fs (attempt %d)", delay, attempt + 1)
    time.sleep(delay)


def linear_transport(
    token: str,
    *,
    client: Any | None = None,
    url: str = LINEAR_GRAPHQL_URL,
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build the ``transport(document, variables) -> data`` Linear expects."""

    def transport(document: str, variables: dict[str, Any]) -> dict[str, Any]:
        http = client or _new_client()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = http.post(
                    url,
                    json={"query": document, "variables": variables},
                    headers={"Authorization": token, "Content-Type": "application/json"},
                )
                if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = _parse_retry_after(response)
                    _retry_sleep(attempt, retry_after)
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise TransportError(f"Linear GraphQL error: {payload['errors']}")
                return payload.get("data", {})
            except TransportError:
                raise
            except Exception as exc:
                # Non-retryable HTTP errors (4xx) propagate immediately.
                if _is_client_error(exc):
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    _retry_sleep(attempt, None)
                    continue
                raise TransportError(f"Linear request failed after {_MAX_RETRIES} retries") from exc
        raise TransportError("Linear request failed") from last_exc  # pragma: no cover

    return transport


def github_transport(
    token: str,
    *,
    client: Any | None = None,
    base: str = GITHUB_API_BASE,
) -> Callable[..., Any]:
    """Build the ``transport(method, path, body=None) -> json`` for GitHub.

    ``body`` (sent as JSON) supports the write paths: issue comments, labels,
    workflow dispatches.
    """

    def transport(method: str, path: str, body: Any | None = None) -> Any:
        http = client or _new_client()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = http.request(
                    method,
                    f"{base}{path}",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = _parse_retry_after(response)
                    _retry_sleep(attempt, retry_after)
                    continue
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            except Exception as exc:
                # Non-retryable HTTP errors (4xx) propagate immediately.
                if _is_client_error(exc):
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    _retry_sleep(attempt, None)
                    continue
                raise TransportError(f"GitHub request failed after {_MAX_RETRIES} retries") from exc
        raise TransportError("GitHub request failed") from last_exc  # pragma: no cover

    return transport


def github_rest_transport(
    token: str,
    *,
    client: Any | None = None,
    base: str = GITHUB_API_BASE,
) -> Callable[..., tuple[int, dict[str, str], Any]]:
    """``transport(method, path) -> (status, headers, json|None)`` for the catalog sync.

    Unlike ``github_transport`` this exposes status and headers, because the sync
    needs pagination metadata and (later) conditional requests. 404 and 409 are
    returned, not raised - missing READMEs are normal, and the Git Trees API
    answers 409 for an empty repository.
    """

    def transport(method: str, path: str) -> tuple[int, dict[str, str], Any]:
        http = client or _new_client()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = http.request(
                    method,
                    f"{base}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = _parse_retry_after(response)
                    _retry_sleep(attempt, retry_after)
                    continue
                # 404/409 are returned, not raised - missing resources are
                # normal, and empty repositories answer 409 on the trees API.
                if response.status_code in (404, 409):
                    return (response.status_code, dict(response.headers), None)
                response.raise_for_status()
                headers = dict(response.headers)
                if response.status_code == 204 or not response.content:
                    return (response.status_code, headers, None)
                return (response.status_code, headers, response.json())
            except Exception as exc:
                if _is_client_error(exc):
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    _retry_sleep(attempt, None)
                    continue
                raise TransportError(
                    f"GitHub request failed after {_MAX_RETRIES} retries"
                ) from exc
        raise TransportError("GitHub request failed") from last_exc  # pragma: no cover

    return transport


def jira_transport(
    base_url: str,
    email: str,
    api_token: str,
    *,
    client: Any | None = None,
) -> Callable[..., Any]:
    """Build the ``transport(method, path, body=None) -> json`` for Jira Cloud.

    Jira Cloud uses basic auth (account email + API token). ``base_url`` is the
    site root, e.g. ``https://yourcompany.atlassian.net``.
    """
    import base64

    auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    root = base_url.rstrip("/")

    def transport(method: str, path: str, body: Any | None = None) -> Any:
        http = client or _new_client()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = http.request(
                    method,
                    f"{root}{path}",
                    json=body,
                    headers={
                        "Authorization": f"Basic {auth}",
                        "Accept": "application/json",
                    },
                )
                if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = _parse_retry_after(response)
                    _retry_sleep(attempt, retry_after)
                    continue
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            except Exception as exc:
                if _is_client_error(exc):
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    _retry_sleep(attempt, None)
                    continue
                raise TransportError(f"Jira request failed after {_MAX_RETRIES} retries") from exc
        raise TransportError("Jira request failed") from last_exc  # pragma: no cover

    return transport


def _is_client_error(exc: Exception) -> bool:
    """True for 4xx HTTP errors (except 429 which is retryable)."""
    try:
        status = exc.response.status_code  # type: ignore[union-attr]
        return 400 <= status < 500 and status != 429
    except AttributeError:
        return False


def _parse_retry_after(response: Any) -> float | None:
    """Extract the Retry-After delay in seconds from a 429 response, if present."""
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def json_post_transport(
    headers: dict[str, str] | None = None, *, client: Any | None = None
) -> Callable[[str, dict[str, Any], dict[str, str]], Any]:
    """``http_post(url, json_body, extra_headers) -> json|None`` for providers.

    Used by the Cursor Cloud and Claude Code providers. Base headers (e.g. the
    Authorization header) are fixed at construction so tokens never travel
    through job inputs.
    """
    base_headers = dict(headers or {})

    def http_post(url: str, body: dict[str, Any], extra: dict[str, str]) -> Any:
        http = client or _new_client()
        response = http.post(url, json=body, headers={**base_headers, **extra})
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    return http_post


def json_get_transport(
    headers: dict[str, str] | None = None, *, client: Any | None = None
) -> Callable[[str, dict[str, str]], Any]:
    """``http_get(url, extra_headers) -> json`` companion to json_post_transport."""
    base_headers = dict(headers or {})

    def http_get(url: str, extra: dict[str, str]) -> Any:
        http = client or _new_client()
        response = http.get(url, headers={**base_headers, **extra})
        response.raise_for_status()
        return response.json()

    return http_get


def raw_post_transport(
    *, client: Any | None = None
) -> Callable[[str, bytes, dict[str, str]], Any]:
    """``http_post(url, raw_body, headers) -> json|None`` for the webhook provider.

    The body is sent verbatim so the receiver can verify the HMAC signature
    against the exact bytes.
    """

    def http_post(url: str, body: bytes, headers: dict[str, str]) -> Any:
        http = client or _new_client()
        response = http.post(url, content=body, headers=headers)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    return http_post


def _new_client() -> Any:  # pragma: no cover - only on the live path
    try:
        import httpx
    except ImportError as exc:
        raise TransportError(
            "httpx is required for live transports; install the 'http' extra"
        ) from exc
    return httpx.Client(timeout=30.0)
