"""Outbound Teams notifications: the approval card + run status updates.

The Teams twin of ``test_slack_notifications.py``. Here we pin (1) that
``TeamsNotifier`` renders the approval card and status updates into the right
Incoming-Webhook payload (fixture-pinned, and carrying the command syntax the
inbound parser round-trips); (2) the ``teams_transport`` wire behaviour; and
(3) that ``MultiNotifier`` fans out to several surfaces best-effort so a Teams
outage can't starve Slack (or break a run).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from foundry.api.teams import parse_teams_interaction
from foundry.connectors import InMemoryNotifier, MultiNotifier, TeamsNotifier
from foundry.connectors.notify import ApprovalProgress, ApprovalRequest
from foundry.connectors.transport import teams_transport
from foundry.schemas.common import RunStatus

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE_REQUEST = ApprovalRequest(
    issue_id="issue-r",
    issue_key="LIN-123",
    title="Add customer favourites",
    work_type="feature",
    risk="medium",
    agent_mode="draft_pr",
    repo="customer-web",
    acceptance_criteria=("A favourites button exists", "Favourites persist across sessions"),
    required_approvals=("engineering",),
)


def _recording_notifier() -> tuple[TeamsNotifier, list[tuple[str, dict]]]:
    sent: list[tuple[str, dict]] = []

    def transport(text: str, card: dict):
        sent.append((text, card))
        return {"status_code": 200}

    return TeamsNotifier(transport), sent


def _card_text(card: dict) -> str:
    return json.dumps(card)


# -- TeamsNotifier rendering ---------------------------------------------------


def test_approval_card_carries_inbound_command_syntax() -> None:
    """The card must show the exact command the inbound parser round-trips.

    The load-bearing contract: an approver who copies the hint into a reply
    drives the same parser the inbound endpoint uses, on the same issue id.
    """
    notifier, sent = _recording_notifier()
    notifier.approval_requested(SAMPLE_REQUEST)
    [(text, card)] = sent
    assert "Add customer favourites" in text

    hint = next(
        b["text"]
        for b in card["body"]
        if b["type"] == "TextBlock" and b["text"].startswith("To decide")
    )
    # The hint embeds the issue_id (not the human key) - that is what resolves.
    assert SAMPLE_REQUEST.issue_id in hint
    # And that exact command parses back to an approve on the run.
    interaction = parse_teams_interaction(
        {"type": "message", "from": {"id": "u"},
         "text": f"<at>Foundry</at> approve {SAMPLE_REQUEST.issue_id}"}
    )
    assert interaction is not None
    assert interaction.command == "approve"
    assert interaction.issue_id == SAMPLE_REQUEST.issue_id


def test_approval_card_matches_fixture() -> None:
    """The full Incoming-Webhook payload is pinned, including the wire envelope."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, text="1")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TeamsNotifier(
        teams_transport("https://example.webhook.office.com/hook", client=client)
    )
    notifier.approval_requested(SAMPLE_REQUEST)

    assert captured["url"] == "https://example.webhook.office.com/hook"
    expected = json.loads((FIXTURES / "teams_card_approval.json").read_text())
    assert captured["body"] == expected


def test_approval_card_surfaces_n_of_m_count() -> None:
    notifier, sent = _recording_notifier()
    notifier.approval_requested(
        ApprovalRequest(
            issue_id="issue-r",
            issue_key="LIN-123",
            title="Touch the ledger",
            work_type="feature",
            risk="high",
            agent_mode="draft_pr",
            repo="payments-service",
            required_approvals=("security",),
            min_approvals=2,
        )
    )
    [(_text, card)] = sent
    rendered = _card_text(card)
    assert "Approvers required" in rendered
    assert "2 distinct sign-offs" in rendered


def test_approval_card_omits_count_for_single_approval() -> None:
    notifier, sent = _recording_notifier()
    notifier.approval_requested(SAMPLE_REQUEST)  # min_approvals defaults to 1
    [(_text, card)] = sent
    assert "Approvers required" not in _card_text(card)


def test_status_message_renders_label() -> None:
    notifier, sent = _recording_notifier()
    notifier.status_changed("issue-r", "LIN-123", RunStatus.PR_OPEN)
    [(text, card)] = sent
    assert "LIN-123" in text
    assert "PR open" in text
    assert card["body"][0]["type"] == "TextBlock"


def test_status_message_falls_back_to_issue_id_without_key() -> None:
    notifier, sent = _recording_notifier()
    notifier.status_changed("issue-r", None, RunStatus.COMPLETE)
    [(text, _card)] = sent
    assert "issue-r" in text
    assert "Merged" in text


def test_approval_progress_message_nudges_next_approver() -> None:
    notifier, sent = _recording_notifier()
    notifier.approval_progress(
        ApprovalProgress(
            issue_id="issue-r",
            issue_key="LIN-123",
            collected=1,
            required=2,
            last_approver="alice@example.com",
        )
    )
    [(text, _card)] = sent
    assert "LIN-123" in text
    assert "1 of 2 approvals collected" in text
    assert "alice@example.com" in text
    assert "1 more distinct sign-off needed" in text


def test_approval_progress_message_pluralises_and_falls_back_to_id() -> None:
    notifier, sent = _recording_notifier()
    notifier.approval_progress(
        ApprovalProgress(
            issue_id="issue-r",
            issue_key=None,
            collected=1,
            required=3,
            last_approver="bob@example.com",
        )
    )
    [(text, _card)] = sent
    assert "issue-r" in text
    assert "2 more distinct sign-offs needed" in text  # plural


# -- teams_transport wire behaviour --------------------------------------------


def test_teams_transport_posts_the_adaptive_card_envelope() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="1")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = teams_transport("https://hook", client=client)
    out = transport("hi", {"type": "AdaptiveCard"})
    assert out["status_code"] == 200
    assert seen["body"]["type"] == "message"
    assert seen["body"]["summary"] == "hi"
    [attachment] = seen["body"]["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"] == {"type": "AdaptiveCard"}


def test_teams_transport_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad webhook url")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = teams_transport("https://hook", client=client)
    with pytest.raises(httpx.HTTPStatusError):
        transport("hi", {})


# -- MultiNotifier fan-out -----------------------------------------------------


def test_multi_notifier_fans_out_to_every_surface() -> None:
    a, b = InMemoryNotifier(), InMemoryNotifier()
    multi = MultiNotifier([a, b])
    multi.approval_requested(SAMPLE_REQUEST)
    multi.status_changed("issue-r", "LIN-123", RunStatus.PR_OPEN)
    multi.approval_progress(
        ApprovalProgress("issue-r", "LIN-123", 1, 2, "alice@example.com")
    )
    for n in (a, b):
        assert len(n.approvals) == 1
        assert len(n.statuses) == 1
        assert len(n.progress) == 1


def test_multi_notifier_isolates_a_failing_surface() -> None:
    """A surface that raises must not starve the others (issue #173)."""

    class Boom:
        def approval_requested(self, request):
            raise RuntimeError("teams down")

        def status_changed(self, issue_id, issue_key, status):
            raise RuntimeError("teams down")

        def approval_progress(self, progress):
            raise RuntimeError("teams down")

    healthy = InMemoryNotifier()
    multi = MultiNotifier([Boom(), healthy])
    # No exception escapes, and the healthy surface still receives everything.
    multi.approval_requested(SAMPLE_REQUEST)
    multi.status_changed("issue-r", "LIN-123", RunStatus.COMPLETE)
    assert len(healthy.approvals) == 1
    assert len(healthy.statuses) == 1
