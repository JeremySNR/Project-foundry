"""Live transport shims tested against httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from foundry.connectors import GitHubConnector, GitLabConnector, LinearConnector
from foundry.connectors.transport import (
    TransportError,
    github_rest_transport,
    github_transport,
    gitlab_transport,
    linear_transport,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


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


# -- GitLab -------------------------------------------------------------------


def test_gitlab_transport_drives_connector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        # The project path must stay percent-encoded on the wire as a single
        # path segment (httpx decodes .path, so assert on the raw URL).
        assert "/projects/acme%2Fcustomer-web/merge_requests/87/diffs" in str(request.url)
        assert request.headers["private-token"] == "gl_token"
        return httpx.Response(
            200,
            json=[{"new_path": "src/a.ts", "old_path": "src/a.ts"}],
        )

    transport = gitlab_transport("gl_token", client=_client(handler))
    files = GitLabConnector(transport=transport).list_mr_files("acme/customer-web", 87)
    assert files == ["src/a.ts"]


def test_gitlab_transport_honours_self_managed_base() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://gitlab.example.com/api/v4/")
        return httpx.Response(200, json=[])

    transport = gitlab_transport(
        "t", client=_client(handler), base="https://gitlab.example.com/api/v4"
    )
    assert transport("GET", "/projects/1/merge_requests/2/diffs") == []


def test_gitlab_transport_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "404 Not found"})

    transport = gitlab_transport("t", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport("GET", "/projects/1/merge_requests/2/diffs")


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
