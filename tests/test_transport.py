"""Live transport shims tested against httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from foundry.connectors import GitHubConnector, LinearConnector
from foundry.connectors.transport import (
    TransportError,
    github_transport,
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
