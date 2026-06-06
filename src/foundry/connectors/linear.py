"""Linear connector.

Implements :class:`IssueTracker` against Linear's GraphQL API. The HTTP/GraphQL
call is injected as a ``transport`` callable - ``transport(document, variables)``
returns the GraphQL ``data`` object - so the connector is fully testable with no
network, and the live wiring (httpx + bearer token) is a thin shim configured at
the edge.

State changes are driven by a ``state_map`` (Foundry state name -> Linear
workflow ``stateId``). You configure these IDs once for your team; this keeps
``set_state`` a single mutation and avoids guessing state names at runtime.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.schemas.ticket import LinkedResource, RawTicket

Transport = Callable[[str, dict[str, Any]], dict[str, Any]]

_ISSUE_QUERY = """
query FoundryIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    labels { nodes { name } }
    attachments { nodes { url } }
  }
}
"""

_COMMENT_MUTATION = """
mutation FoundryComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""

_STATE_MUTATION = """
mutation FoundryState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) { success }
}
"""

_REPO_LABEL_PREFIX = "repo:"


class LinearConnector:
    def __init__(
        self,
        *,
        transport: Transport,
        state_map: dict[str, str] | None = None,
    ) -> None:
        self._transport = transport
        # Foundry state name -> Linear workflow state id.
        self._state_map = state_map or {}

    def get_issue(self, issue_id: str) -> RawTicket:
        data = self._transport(_ISSUE_QUERY, {"id": issue_id})
        issue = data["issue"]
        labels = [
            node["name"]
            for node in (issue.get("labels", {}) or {}).get("nodes", [])
            if node.get("name")
        ]
        known_repos = [
            lab[len(_REPO_LABEL_PREFIX) :]
            for lab in labels
            if lab.startswith(_REPO_LABEL_PREFIX)
        ]
        attachments = [
            node["url"]
            for node in (issue.get("attachments", {}) or {}).get("nodes", [])
            if node.get("url")
        ]
        linked = [
            LinkedResource(kind="attachment", url=url)
            for url in attachments
        ]
        return RawTicket(
            issue_id=str(issue["id"]),
            issue_key=str(issue.get("identifier") or ""),
            title=issue.get("title") or "",
            description=issue.get("description") or "",
            labels=labels,
            known_repositories=known_repos,
            linked_resources=linked,
        )

    def post_comment(self, issue_id: str, body: str) -> None:
        self._transport(_COMMENT_MUTATION, {"issueId": issue_id, "body": body})

    def set_state(self, issue_id: str, state_name: str) -> None:
        state_id = self._state_map.get(state_name)
        if state_id is None:
            # No configured mapping for this state; skip rather than guess.
            return
        self._transport(_STATE_MUTATION, {"issueId": issue_id, "stateId": state_id})
