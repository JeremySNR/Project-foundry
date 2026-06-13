"""Live HTTP transports for the connectors.

The connectors take an injected ``transport`` callable so they stay testable.
These factories produce the real ones (httpx) that talk to Linear's GraphQL API
and GitHub's REST API. ``httpx`` is imported lazily so it is only required when
you actually wire a live connector; tests pass a client built on
``httpx.MockTransport`` and never hit the network.

Retry safety
------------
Transient failures (network errors, 5xx, 429) are retried with backoff, but
**only for idempotent operations**. Replaying a non-idempotent write after a
502 that the server actually processed would duplicate a Linear/Jira/GitHub
comment or, worse, double-fire a ``workflow_dispatch``/webhook agent dispatch —
a second real (and billable) agent run outside the orchestrator's retry-cap
accounting. So we never retry a write we can't prove is safe to repeat:

- REST transports derive idempotency from the HTTP method: ``GET``/``HEAD``/
  ``PUT``/``DELETE`` are idempotent (per RFC 7231) and retried; ``POST``/
  ``PATCH`` are not.
- The Linear transport (always a GraphQL POST) derives it from the operation:
  a ``query`` is retried, a ``mutation`` is not.
"""

from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
GITHUB_API_BASE = "https://api.github.com"
GITLAB_API_BASE = "https://gitlab.com/api/v4"

_log = logging.getLogger(__name__)

# Statuses that are safe to retry (transient server errors / rate limits).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# HTTP methods whose repetition leaves the server in the same state (RFC 7231).
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds


class TransportError(RuntimeError):
    """Raised when an upstream API returns an error."""


def _retry_sleep(attempt: int, retry_after: float | None) -> None:
    delay = retry_after if retry_after is not None else _BACKOFF_BASE ** attempt
    _log.warning("upstream request failed; retrying in %.1fs (attempt %d)", delay, attempt + 1)
    time.sleep(delay)


def _is_idempotent_method(method: str) -> bool:
    return method.upper() in _IDEMPOTENT_METHODS


def _graphql_is_idempotent(document: str) -> bool:
    """True if a GraphQL document is a read (``query``), so safe to retry.

    Anonymous shorthand (``{ ... }``) is a query. A ``mutation`` (or anything we
    can't positively identify as a query) is treated as non-idempotent: a
    misread read merely loses a retry, but a retried mutation could duplicate a
    comment or state change. We fail safe toward "do not retry".
    """
    for raw_line in document.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            return True
        token = line.split(None, 1)[0].lower()
        return token == "query"
    return False


def _client_provider(client: Any | None) -> Callable[[], Any]:
    """Return a ``() -> http_client`` that reuses one client on the live path.

    An injected client (the tests' ``httpx.MockTransport``) is returned as-is.
    Otherwise a single live ``httpx.Client`` is created lazily and reused across
    every call the factory makes — connection pooling matters across a
    multi-thousand-call catalog sync.
    """
    if client is not None:
        return lambda: client
    cache: dict[str, Any] = {}

    def get() -> Any:
        if "client" not in cache:
            cache["client"] = _new_client()
        return cache["client"]

    return get


def linear_transport(
    token: str,
    *,
    client: Any | None = None,
    url: str = LINEAR_GRAPHQL_URL,
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build the ``transport(document, variables) -> data`` Linear expects."""
    get_client = _client_provider(client)

    def transport(document: str, variables: dict[str, Any]) -> dict[str, Any]:
        http = get_client()
        idempotent = _graphql_is_idempotent(document)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = http.post(
                    url,
                    json={"query": document, "variables": variables},
                    headers={"Authorization": token, "Content-Type": "application/json"},
                )
                if (
                    idempotent
                    and _retryable_status(response)
                    and attempt < _MAX_RETRIES
                ):
                    _retry_sleep(attempt, _retry_after_seconds(response))
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise TransportError(f"Linear GraphQL error: {payload['errors']}")
                return payload.get("data", {})
            except TransportError:
                raise
            except Exception as exc:
                # 4xx and any non-idempotent failure propagate immediately.
                if _is_client_error(exc) or not idempotent:
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
    workflow dispatches. Only idempotent methods are retried (see module docs).
    """
    get_client = _client_provider(client)

    def transport(method: str, path: str, body: Any | None = None) -> Any:
        http = get_client()
        idempotent = _is_idempotent_method(method)
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
                if (
                    idempotent
                    and _retryable_status(response)
                    and attempt < _MAX_RETRIES
                ):
                    _retry_sleep(attempt, _retry_after_seconds(response))
                    continue
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            except Exception as exc:
                # 4xx and any non-idempotent failure propagate immediately.
                if _is_client_error(exc) or not idempotent:
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
    answers 409 for an empty repository. The sync only ever issues ``GET``s, so
    every call is idempotent and retried.
    """
    get_client = _client_provider(client)

    def transport(method: str, path: str) -> tuple[int, dict[str, str], Any]:
        http = get_client()
        idempotent = _is_idempotent_method(method)
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
                if (
                    idempotent
                    and _retryable_status(response)
                    and attempt < _MAX_RETRIES
                ):
                    _retry_sleep(attempt, _retry_after_seconds(response))
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
                if _is_client_error(exc) or not idempotent:
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


def gitlab_transport(
    token: str,
    *,
    client: Any | None = None,
    base: str = GITLAB_API_BASE,
) -> Callable[..., Any]:
    """Build the ``transport(method, path) -> json`` for the GitLab REST API.

    Used to fetch MR diffs so the changed-file list feeds the same forbidden-path
    and oversize gates GitHub PRs already get. Personal/project access tokens go
    in ``PRIVATE-TOKEN``; ``base`` is the API root (override for self-managed,
    e.g. ``https://gitlab.example.com/api/v4``). Only idempotent methods are
    retried (see module docs); the diff reads are GETs.
    """
    get_client = _client_provider(client)

    def transport(method: str, path: str) -> Any:
        http = get_client()
        idempotent = _is_idempotent_method(method)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = http.request(
                    method,
                    f"{base}{path}",
                    headers={
                        "PRIVATE-TOKEN": token,
                        "Accept": "application/json",
                    },
                )
                if (
                    idempotent
                    and _retryable_status(response)
                    and attempt < _MAX_RETRIES
                ):
                    _retry_sleep(attempt, _retry_after_seconds(response))
                    continue
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            except Exception as exc:
                if _is_client_error(exc) or not idempotent:
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    _retry_sleep(attempt, None)
                    continue
                raise TransportError(f"GitLab request failed after {_MAX_RETRIES} retries") from exc
        raise TransportError("GitLab request failed") from last_exc  # pragma: no cover

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
    site root, e.g. ``https://yourcompany.atlassian.net``. Only idempotent
    methods are retried (see module docs).
    """
    import base64

    auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    root = base_url.rstrip("/")
    get_client = _client_provider(client)

    def transport(method: str, path: str, body: Any | None = None) -> Any:
        http = get_client()
        idempotent = _is_idempotent_method(method)
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
                if (
                    idempotent
                    and _retryable_status(response)
                    and attempt < _MAX_RETRIES
                ):
                    _retry_sleep(attempt, _retry_after_seconds(response))
                    continue
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            except Exception as exc:
                if _is_client_error(exc) or not idempotent:
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    _retry_sleep(attempt, None)
                    continue
                raise TransportError(f"Jira request failed after {_MAX_RETRIES} retries") from exc
        raise TransportError("Jira request failed") from last_exc  # pragma: no cover

    return transport


def _retryable_status(response: Any) -> bool:
    """True if a response status warrants a retry (for idempotent requests).

    Covers the transient 5xx/429 set plus GitHub's secondary rate limit, which
    answers ``403`` with a ``Retry-After`` header rather than a 429.
    """
    status = response.status_code
    if status in _RETRYABLE_STATUSES:
        return True
    return status == 403 and response.headers.get("Retry-After") is not None


def _is_client_error(exc: Exception) -> bool:
    """True for 4xx HTTP errors (except 429 which is retryable)."""
    try:
        status = exc.response.status_code  # type: ignore[union-attr]
        return 400 <= status < 500 and status != 429
    except AttributeError:
        return False


def _retry_after_seconds(response: Any) -> float | None:
    """Extract the Retry-After delay in seconds, if present.

    Honours both forms RFC 7231 allows: delta-seconds (``120``) and an HTTP-date
    (``Wed, 21 Oct 2015 07:28:00 GMT``), which GitHub uses for rate limits.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(when.tzinfo) if when.tzinfo else _dt.datetime.now()
    delta = (when - now).total_seconds()
    return max(delta, 0.0)


def json_post_transport(
    headers: dict[str, str] | None = None, *, client: Any | None = None
) -> Callable[[str, dict[str, Any], dict[str, str]], Any]:
    """``http_post(url, json_body, extra_headers) -> json|None`` for providers.

    Used by the Cursor Cloud and Claude Code providers. Base headers (e.g. the
    Authorization header) are fixed at construction so tokens never travel
    through job inputs. No retry: these POSTs dispatch coding agents, so a
    blind replay would double-fire a billable run.
    """
    base_headers = dict(headers or {})
    get_client = _client_provider(client)

    def http_post(url: str, body: dict[str, Any], extra: dict[str, str]) -> Any:
        http = get_client()
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
    get_client = _client_provider(client)

    def http_get(url: str, extra: dict[str, str]) -> Any:
        http = get_client()
        response = http.get(url, headers={**base_headers, **extra})
        response.raise_for_status()
        return response.json()

    return http_get


def raw_post_transport(
    *, client: Any | None = None
) -> Callable[[str, bytes, dict[str, str]], Any]:
    """``http_post(url, raw_body, headers) -> json|None`` for the webhook provider.

    The body is sent verbatim so the receiver can verify the HMAC signature
    against the exact bytes. No retry: this POST dispatches a coding agent, so a
    blind replay would double-fire a billable run.
    """
    get_client = _client_provider(client)

    def http_post(url: str, body: bytes, headers: dict[str, str]) -> Any:
        http = get_client()
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
