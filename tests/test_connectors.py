"""Tests for the connector layer (Linear adapter + comment/state rendering)."""

from __future__ import annotations

import pytest

from foundry.connectors import (
    InMemoryIssueTracker,
    LinearConnector,
    LinearWriteError,
    format_analysis_comment,
    format_approval_progress_comment,
    format_cursor_delegation,
    state_for,
)
from foundry.engines import (
    HeuristicAnalyzer,
    HeuristicRiskClassifier,
    StaticContextEnricher,
    TemplatePlanner,
)
from foundry.schemas.common import RunStatus
from foundry.schemas.ticket import RawTicket


class RecordingTransport:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or []

    def __call__(self, document: str, variables: dict) -> dict:
        self.calls.append((document, variables))
        return self._responses.pop(0) if self._responses else {}


# -- LinearConnector ----------------------------------------------------------


def test_get_issue_maps_to_raw_ticket() -> None:
    transport = RecordingTransport(
        [
            {
                "issue": {
                    "id": "uuid-1",
                    "identifier": "LIN-7",
                    "title": "Add favourites",
                    "description": "do it",
                    "labels": {"nodes": [{"name": "repo:customer-web"}, {"name": "x"}]},
                    "attachments": {"nodes": [{"url": "https://github.com/o/r/pull/3"}]},
                }
            }
        ]
    )
    connector = LinearConnector(transport=transport)
    ticket = connector.get_issue("uuid-1")
    assert ticket.issue_key == "LIN-7"
    assert ticket.known_repositories == ["customer-web"]
    assert ticket.linked_resources[0].url.endswith("/pull/3")
    assert transport.calls[0][1] == {"id": "uuid-1"}


def test_post_comment_calls_mutation() -> None:
    transport = RecordingTransport()
    LinearConnector(transport=transport).post_comment("uuid-1", "hello")
    document, variables = transport.calls[0]
    assert "commentCreate" in document
    assert variables == {"issueId": "uuid-1", "body": "hello"}


def test_set_state_uses_state_map() -> None:
    transport = RecordingTransport()
    connector = LinearConnector(
        transport=transport, state_map={"Foundry: Blocked": "state-123"}
    )
    connector.set_state("uuid-1", "Foundry: Blocked")
    document, variables = transport.calls[0]
    assert "issueUpdate" in document
    assert variables == {"issueId": "uuid-1", "stateId": "state-123"}


def test_set_state_unmapped_is_skipped() -> None:
    transport = RecordingTransport()
    LinearConnector(transport=transport).set_state("uuid-1", "Foundry: Unknown")
    assert transport.calls == []  # no guessing


def test_comment_success_false_raises() -> None:
    """A success:false mutation (no GraphQL errors) must not be silently dropped."""
    transport = RecordingTransport([{"commentCreate": {"success": False}}])
    with pytest.raises(LinearWriteError):
        LinearConnector(transport=transport).post_comment("uuid-1", "hello")


def test_set_state_success_false_raises() -> None:
    transport = RecordingTransport([{"issueUpdate": {"success": False}}])
    connector = LinearConnector(
        transport=transport, state_map={"Foundry: Blocked": "state-123"}
    )
    with pytest.raises(LinearWriteError):
        connector.set_state("uuid-1", "Foundry: Blocked")


def test_set_state_success_true_is_accepted() -> None:
    transport = RecordingTransport([{"issueUpdate": {"success": True}}])
    connector = LinearConnector(
        transport=transport, state_map={"Foundry: Blocked": "state-123"}
    )
    connector.set_state("uuid-1", "Foundry: Blocked")  # no raise


# -- InMemoryIssueTracker -----------------------------------------------------


def test_in_memory_tracker_records() -> None:
    tracker = InMemoryIssueTracker()
    tracker.add_issue(RawTicket(issue_id="i", issue_key="LIN-1", title="t"))
    tracker.post_comment("i", "hi")
    tracker.set_state("i", "Foundry: Analysing")
    assert tracker.comments["i"] == ["hi"]
    assert tracker.states["i"] == "Foundry: Analysing"
    assert tracker.get_issue("i").issue_key == "LIN-1"


# -- comment / state rendering ------------------------------------------------


def _artifacts(desc: str, repos: list[str]):
    ticket = RawTicket(
        issue_id="i", issue_key="LIN-1", title="Add favourites",
        description=desc, known_repositories=repos,
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    plan = TemplatePlanner().plan(ticket, analysis, context, risk)
    return analysis, risk, plan


def test_analysis_comment_for_waiting_approval_has_commands() -> None:
    analysis, risk, plan = _artifacts(
        "Acceptance Criteria:\n- a button exists\n- it persists", ["customer-web"]
    )
    body = format_analysis_comment(analysis, risk, plan, RunStatus.WAITING_APPROVAL)
    assert "Foundry analysis complete" in body
    assert "/foundry approve" in body
    assert "customer-web" in body


def test_analysis_comment_surfaces_n_of_m_when_required() -> None:
    """When the run needs >1 distinct approver, the comment says so up front, so
    the first approver isn't surprised by a run that stays parked (issue #31)."""
    analysis, risk, plan = _artifacts(
        "Acceptance Criteria:\n- a button exists\n- it persists", ["customer-web"]
    )
    body = format_analysis_comment(
        analysis, risk, plan, RunStatus.WAITING_APPROVAL, min_approvals=2
    )
    assert "Approvers required:" in body
    assert "2 distinct sign-offs" in body


def test_analysis_comment_omits_n_of_m_for_single_approval() -> None:
    """The default single-approval prompt is unchanged - no N-of-M line."""
    analysis, risk, plan = _artifacts(
        "Acceptance Criteria:\n- a button exists\n- it persists", ["customer-web"]
    )
    body = format_analysis_comment(
        analysis, risk, plan, RunStatus.WAITING_APPROVAL, min_approvals=1
    )
    assert "Approvers required:" not in body


def test_analysis_comment_for_needs_clarification() -> None:
    analysis, risk, plan = _artifacts("vague", [])
    body = format_analysis_comment(analysis, risk, plan, RunStatus.NEEDS_CLARIFICATION)
    assert "Needs clarification" in body
    # Clarification does work for the author: a copy-editable draft, not a bounce.
    assert "Suggested acceptance criteria" in body
    assert "Given <starting state>" in body


def test_state_for_known_and_unknown() -> None:
    assert state_for(RunStatus.WAITING_APPROVAL) == "Foundry: Waiting Approval"
    assert state_for(RunStatus.PR_OPEN) == "Foundry: PR Open"


def test_cursor_delegation_mentions_cursor() -> None:
    body = format_cursor_delegation("Implement the favourites feature.")
    assert body.startswith("@Cursor")
    assert "Implement the favourites feature." in body


def test_approval_progress_comment_singular_remaining() -> None:
    body = format_approval_progress_comment(
        collected=1, required=2, last_approver="alice@example.com"
    )
    assert "approval progress" in body.lower()
    assert "1 of 2 distinct sign-offs collected (latest: alice@example.com)" in body
    assert "1 more approver required" in body  # singular
    assert "/foundry approve" in body


def test_approval_progress_comment_plural_remaining() -> None:
    body = format_approval_progress_comment(
        collected=1, required=3, last_approver="bob@example.com"
    )
    assert "2 more approvers required" in body  # plural
