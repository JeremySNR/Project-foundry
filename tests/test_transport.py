"""Live transport shims tested against httpx.MockTransport (no network)."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from foundry.connectors import GitHubConnector, LinearConnector
from foundry.connectors import transport as transport_mod
from foundry.connectors.transport import (
    TransportError,
    github_rest_transport,
    github_transport,
    jira_transport,
    linear_transport,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> None:
    """Retries must not actually sleep during tests."""
    monkeypatch.setattr(transport_mod, "_retry_sleep", lambda *a, **k: None)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _counting(responses):
    """A MockTransport handler that yields the given responses in order.

    Returns ``(handler, calls)`` where ``calls`` is a list of the requests seen,
    so a test can assert exactly how many times the transport hit the wire.
    """
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return queue.pop(0) if queue else httpx.Response(200, json={})

    return handler, calls


# -- Linear -------------------------------------------------------------------


def test_linear_transport_drives_connector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.linear.app/graphql"
        assert request.headers["authorization"] == "lin_token"
        body = json.loads(request.content)
        assert "query" in body and "variables" in body
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "id": "uuid-1",
                        "identifier": "LIN-9",
                        "title": "T",
                        "description": "D",
                        "labels": {"nodes": [{"name": "repo:web"}]},
                        "attachments": {"nodes": []},
                    }
                }
            },
        )

    transport = linear_transport("lin_token", client=_client(handler))
    ticket = LinearConnector(transport=transport).get_issue("uuid-1")
    assert ticket.issue_key == "LIN-9"
    assert ticket.known_repositories == ["web"]


def test_linear_transport_raises_on_graphql_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "nope"}]})

    transport = linear_transport("t", client=_client(handler))
    with pytest.raises(TransportError):
        transport("query {}", {})


# -- GitHub -------------------------------------------------------------------


def test_github_transport_drives_connector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/o/customer-web/pulls/7/files"
        assert request.headers["authorization"] == "Bearer gh_token"
        return httpx.Response(200, json=[{"filename": "src/a.ts"}])

    transport = github_transport("gh_token", client=_client(handler))
    files = GitHubConnector(transport=transport).list_pr_files("o/customer-web", 7)
    assert files == ["src/a.ts"]


def test_github_transport_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    transport = github_transport("t", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport("GET", "/repos/o/r/pulls/1/files")


def test_github_rest_transport_returns_404_and_409_as_values() -> None:
    """Missing resources (404) and empty repos (409 on the trees API) are
    answers, not errors - the catalog sync must see them, not an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        return httpx.Response(404, json={"message": "not found"})

    transport = github_rest_transport("t", client=_client(handler))
    status, _, data = transport("GET", "/repos/o/empty/git/trees/HEAD?recursive=1")
    assert status == 409
    assert data is None
    status, _, data = transport("GET", "/repos/o/r/readme")
    assert status == 404
    assert data is None


# -- retry idempotency --------------------------------------------------------


def test_github_get_retries_transient_then_succeeds() -> None:
    handler, calls = _counting(
        [httpx.Response(502, text="bad gateway"), httpx.Response(200, json=[{"x": 1}])]
    )
    transport = github_transport("t", client=_client(handler))
    assert transport("GET", "/repos/o/r/pulls/1/files") == [{"x": 1}]
    assert len(calls) == 2  # retried once


def test_github_post_is_not_retried_on_transient_error() -> None:
    """A 502 after a POST may mean the server already created the comment /
    dispatch; replaying it would duplicate the side effect, so we surface the
    error after exactly one attempt instead of retrying."""
    handler, calls = _counting(
        [httpx.Response(502, text="bad gateway"), httpx.Response(200, json={})]
    )
    transport = github_transport("t", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport("POST", "/repos/o/r/issues/1/comments", {"body": "hi"})
    assert len(calls) == 1  # no replay of a non-idempotent write


def test_github_post_network_error_is_not_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("boom", request=request)

    transport = github_transport("t", client=_client(handler))
    with pytest.raises(httpx.ConnectError):
        transport("POST", "/repos/o/r/issues/1/comments", {"body": "hi"})
    assert len(calls) == 1


def test_github_put_is_idempotent_and_retried() -> None:
    """Label replacement (the GitHub Issues set_state path) is a PUT of the full
    set - idempotent, so retrying a transient failure is safe."""
    handler, calls = _counting(
        [httpx.Response(503), httpx.Response(200, json={})]
    )
    transport = github_transport("t", client=_client(handler))
    transport("PUT", "/repos/o/r/issues/1/labels", {"labels": ["a"]})
    assert len(calls) == 2


def test_github_secondary_rate_limit_403_retried_for_get() -> None:
    handler, calls = _counting(
        [
            httpx.Response(403, headers={"Retry-After": "0"}, text="secondary limit"),
            httpx.Response(200, json=[]),
        ]
    )
    transport = github_transport("t", client=_client(handler))
    assert transport("GET", "/repos/o/r/pulls/1/files") == []
    assert len(calls) == 2


def test_github_plain_403_is_not_retried() -> None:
    """A 403 without Retry-After is an auth/permission failure, not a rate
    limit - propagate it immediately."""
    handler, calls = _counting([httpx.Response(403, json={"message": "forbidden"})])
    transport = github_transport("t", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport("GET", "/repos/o/r/pulls/1/files")
    assert len(calls) == 1


def test_linear_query_is_retried() -> None:
    handler, calls = _counting(
        [httpx.Response(502), httpx.Response(200, json={"data": {"issue": {}}})]
    )
    transport = linear_transport("t", client=_client(handler))
    assert transport("query Foo { issue { id } }", {}) == {"issue": {}}
    assert len(calls) == 2


def test_linear_mutation_is_not_retried() -> None:
    handler, calls = _counting(
        [httpx.Response(502), httpx.Response(200, json={"data": {}})]
    )
    transport = linear_transport("t", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport("mutation Foo { commentCreate { success } }", {})
    assert len(calls) == 1


def test_jira_post_is_not_retried() -> None:
    handler, calls = _counting([httpx.Response(503)])
    transport = jira_transport("https://x.atlassian.net", "e", "t", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport("POST", "/rest/api/2/issue/X-1/comment", {"body": "hi"})
    assert len(calls) == 1


def test_graphql_idempotency_classifier() -> None:
    assert transport_mod._graphql_is_idempotent("query Foo { a }")
    assert transport_mod._graphql_is_idempotent("\n  # comment\n  { a }\n")  # anonymous
    assert not transport_mod._graphql_is_idempotent("mutation Foo { a }")
    assert not transport_mod._graphql_is_idempotent("")  # unknown => fail safe


# -- Retry-After parsing ------------------------------------------------------


def test_retry_after_delta_seconds() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "12"})
    assert transport_mod._retry_after_seconds(resp) == 12.0


def test_retry_after_http_date() -> None:
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    resp = httpx.Response(429, headers={"Retry-After": header})
    seconds = transport_mod._retry_after_seconds(resp)
    assert seconds is not None and 0 <= seconds <= 31


def test_retry_after_absent_or_garbage() -> None:
    assert transport_mod._retry_after_seconds(httpx.Response(429)) is None
    resp = httpx.Response(429, headers={"Retry-After": "not-a-date"})
    assert transport_mod._retry_after_seconds(resp) is None


# -- connection reuse ---------------------------------------------------------


def test_live_client_is_created_once_and_reused(monkeypatch) -> None:
    created: list[object] = []

    def fake_new_client():
        client = object()
        created.append(client)
        return client

    monkeypatch.setattr(transport_mod, "_new_client", fake_new_client)
    get_client = transport_mod._client_provider(None)
    first = get_client()
    second = get_client()
    assert first is second
    assert len(created) == 1


def test_injected_client_is_used_as_is() -> None:
    sentinel = object()
    get_client = transport_mod._client_provider(sentinel)
    assert get_client() is sentinel
