"""Connector protocols.

Foundry sits *above* the tools it coordinates and talks to each through a thin
adapter. ``IssueTracker`` is the surface the orchestrator needs from a planning
tool (Linear today): read the issue, write progress back, and move its state.

Keeping this a Protocol means the orchestrator never imports Linear directly, and
tests use an in-memory fake.
"""

from __future__ import annotations

from typing import Protocol

from foundry.schemas.ticket import RawTicket


class IssueTracker(Protocol):
    def get_issue(self, issue_id: str) -> RawTicket: ...

    def post_comment(self, issue_id: str, body: str) -> None: ...

    def set_state(self, issue_id: str, state_name: str) -> None: ...


class InMemoryIssueTracker:
    """Test double that records comments and state changes per issue."""

    def __init__(self, issues: dict[str, RawTicket] | None = None) -> None:
        self._issues = issues or {}
        self.comments: dict[str, list[str]] = {}
        self.states: dict[str, str] = {}

    def add_issue(self, ticket: RawTicket) -> None:
        self._issues[ticket.issue_id] = ticket

    def get_issue(self, issue_id: str) -> RawTicket:
        return self._issues[issue_id]

    def post_comment(self, issue_id: str, body: str) -> None:
        self.comments.setdefault(issue_id, []).append(body)

    def set_state(self, issue_id: str, state_name: str) -> None:
        self.states[issue_id] = state_name
