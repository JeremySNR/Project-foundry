"""Outbound Slack notifications: the approval message + run status updates.

The mirror of ``test_slack_approvals.py`` (which covers the *inbound* click). Here
we pin (1) that ``SlackNotifier`` renders the approval message and status updates
into the right ``chat.postMessage`` payload, fixture-pinned and round-tripping
through the inbound parser; (2) the ``slack_transport`` wire behaviour; and (3)
that the orchestrator fires the notifier on the notable lifecycle transitions
only - best-effort, never breaking a run.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api.slack import parse_slack_interaction
from foundry.connectors import InMemoryNotifier, SlackNotifier
from foundry.connectors.notify import ApprovalProgress, ApprovalRequest
from foundry.connectors.transport import TransportError, slack_transport
from foundry.db import (
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import PRStatus, RunStatus
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

FIXTURES = Path(__file__).parent / "fixtures"

READY_DESC = """\
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
"""

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


# -- SlackNotifier rendering ---------------------------------------------------


def _recording_notifier() -> tuple[SlackNotifier, list[tuple[str, list[dict]]]]:
    sent: list[tuple[str, list[dict]]] = []

    def transport(text: str, blocks: list[dict]):
        sent.append((text, blocks))
        return {"ok": True}

    return SlackNotifier(transport), sent


def test_approval_message_buttons_match_inbound_contract() -> None:
    """The buttons the approval message emits parse back to a decision on the run.

    This is the load-bearing contract: a click on the posted message must round
    trip through the same parser the inbound endpoint uses.
    """
    notifier, sent = _recording_notifier()
    notifier.approval_requested(SAMPLE_REQUEST)
    [(text, blocks)] = sent
    assert "Add customer favourites" in text

    actions = next(b for b in blocks if b["type"] == "actions")
    by_command = {}
    for el in actions["elements"]:
        # Reconstruct the inbound block_actions shape Slack would deliver.
        interaction = parse_slack_interaction(
            {
                "type": "block_actions",
                "user": {"id": "U07APPROVER"},
                "actions": [el],
            }
        )
        assert interaction is not None
        assert interaction.issue_id == SAMPLE_REQUEST.issue_id
        by_command[interaction.command] = el["value"]
    assert set(by_command) == {"approve", "reject", "stop"}


def test_approval_message_matches_fixture() -> None:
    """The full chat.postMessage payload is pinned, including the wire envelope."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"ok": True, "ts": "1718370000.000100"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = SlackNotifier(
        slack_transport("xoxb-test-token", "C01APPROVALS", client=client)
    )
    notifier.approval_requested(SAMPLE_REQUEST)

    assert captured["url"] == "https://slack.com/api/chat.postMessage"
    assert captured["auth"] == "Bearer xoxb-test-token"
    expected = json.loads((FIXTURES / "slack_post_message_approval.json").read_text())
    assert captured["body"] == expected


def test_approval_message_surfaces_n_of_m_count() -> None:
    """A run needing >1 distinct approver advertises the count in the message, so
    a Slack approver knows one sign-off won't release the run (issue #31)."""
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
    [(_text, blocks)] = sent
    rendered = json.dumps(blocks)
    assert "Approvers required:" in rendered
    assert "2 distinct sign-offs" in rendered


def test_approval_message_omits_count_for_single_approval() -> None:
    """The default single-approval message is unchanged - no count field."""
    notifier, sent = _recording_notifier()
    notifier.approval_requested(SAMPLE_REQUEST)  # min_approvals defaults to 1
    [(_text, blocks)] = sent
    assert "Approvers required:" not in json.dumps(blocks)


def test_status_message_renders_label() -> None:
    notifier, sent = _recording_notifier()
    notifier.status_changed("issue-r", "LIN-123", RunStatus.PR_OPEN)
    [(text, blocks)] = sent
    assert "LIN-123" in text
    assert "PR open" in text
    assert blocks and blocks[0]["type"] == "section"


def test_status_message_falls_back_to_issue_id_without_key() -> None:
    notifier, sent = _recording_notifier()
    notifier.status_changed("issue-r", None, RunStatus.COMPLETE)
    [(text, _blocks)] = sent
    assert "issue-r" in text
    assert "Merged" in text


def test_approval_progress_message_nudges_next_approver() -> None:
    """A partial N-of-M sign-off renders a progress nudge telling the next
    approver how many distinct sign-offs are in and how many remain (issue #31)."""
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
    [(text, blocks)] = sent
    assert "LIN-123" in text
    assert "1 of 2 approvals collected" in text
    assert "alice@example.com" in text
    # remaining == 1 -> singular "sign-off"
    assert "1 more distinct sign-off needed" in text
    assert blocks and blocks[0]["type"] == "section"


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
    [(text, _blocks)] = sent
    assert "issue-r" in text  # no key -> fall back to the id
    assert "1 of 3 approvals collected" in text
    assert "2 more distinct sign-offs needed" in text  # plural


# -- slack_transport wire behaviour --------------------------------------------


def test_slack_transport_posts_channel_text_and_blocks() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "ts": "1.2"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = slack_transport("xoxb-tok", "C99", client=client)
    out = transport("hi", [{"type": "section"}])
    assert out["ok"] is True
    assert seen["body"]["channel"] == "C99"
    assert seen["body"]["text"] == "hi"
    assert seen["body"]["blocks"] == [{"type": "section"}]


def test_slack_transport_raises_on_logical_error() -> None:
    """Slack answers 200 with ok=false for a bad token/channel; surface it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = slack_transport("xoxb-tok", "C99", client=client)
    with pytest.raises(TransportError, match="channel_not_found"):
        transport("hi", [])


# -- orchestrator wiring -------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _ready_ticket(**overrides) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )
    base.update(overrides)
    return RawTicket(**base)


def _status(session_factory, run_id: str) -> RunStatus:
    with session_factory() as s:
        return s.get(FoundryRun, run_id).status


def test_intake_posts_approval_message(session_factory) -> None:
    notifier = InMemoryNotifier()
    orch = FoundryOrchestrator(session_factory, notifier=notifier)
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    assert len(notifier.approvals) == 1
    req = notifier.approvals[0]
    assert req.issue_id == "i-1"
    assert req.issue_key == "LIN-123"
    assert req.title == "Add customer favourites"
    assert req.acceptance_criteria  # carried through from the analysis
    # A run parked for approval is not also announced as a status change.
    assert notifier.statuses == []


def test_intake_threads_effective_min_approvals_into_message(session_factory) -> None:
    """The orchestrator surfaces the effective N-of-M count (global raised by any
    per-repo override) in the approval message it posts (issue #31)."""
    notifier = InMemoryNotifier()
    orch = FoundryOrchestrator(
        session_factory,
        notifier=notifier,
        min_approvals=1,
        repo_min_approvals={"customer-web": 2},
    )
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    assert len(notifier.approvals) == 1
    assert notifier.approvals[0].min_approvals == 2


def test_intake_message_min_approvals_defaults_to_one(session_factory) -> None:
    """With no N-of-M config the message carries the historical count of 1."""
    notifier = InMemoryNotifier()
    orch = FoundryOrchestrator(session_factory, notifier=notifier)
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    assert notifier.approvals[0].min_approvals == 1


def test_intake_unready_notifies_parked(session_factory) -> None:
    notifier = InMemoryNotifier()
    orch = FoundryOrchestrator(session_factory, notifier=notifier)
    # No acceptance criteria => NEEDS_CLARIFICATION (parked).
    orch.intake_and_plan(
        _ready_ticket(description="Make it nicer.", known_repositories=[]),
        trigger_type="label",
    )
    assert notifier.approvals == []
    assert [s for _i, _k, s in notifier.statuses] == [RunStatus.NEEDS_CLARIFICATION]
    # The intake path threads the issue key through (unlike later transitions).
    assert notifier.statuses[0][1] == "LIN-123"


def test_happy_path_notifies_pr_open_and_merged_not_routine(session_factory) -> None:
    provider = InMemoryFakeProvider()
    notifier = InMemoryNotifier()
    orch = FoundryOrchestrator(session_factory, provider=provider, notifier=notifier)

    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING

    final = provider.run(job.job_id)
    pr = PullRequestState(
        repo="customer-web",
        pr_number=1,
        url=final.pr_url,
        branch=final.branch,
        status=PRStatus.OPEN,
        files_changed=["src/features/favourites/index.ts"],
    )
    orch.record_pr(run_id, pr)
    merged = pr.model_copy(update={"status": PRStatus.MERGED})
    orch.record_pr(run_id, merged)
    assert _status(session_factory, run_id) is RunStatus.COMPLETE

    notified = [s for _i, _k, s in notifier.statuses]
    # APPROVED and AGENT_RUNNING are routine and must not be announced.
    assert RunStatus.APPROVED not in notified
    assert RunStatus.AGENT_RUNNING not in notified
    assert RunStatus.PR_OPEN in notified
    assert RunStatus.COMPLETE in notified


def test_notifier_failure_does_not_break_the_run(session_factory) -> None:
    class Boom:
        def approval_requested(self, request):
            raise RuntimeError("slack down")

        def status_changed(self, issue_id, issue_key, status):
            raise RuntimeError("slack down")

        def approval_progress(self, progress):
            raise RuntimeError("slack down")

    orch = FoundryOrchestrator(session_factory, notifier=Boom(), min_approvals=2)
    # Intake still completes and parks the run despite the notifier raising.
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
    # A partial N-of-M sign-off fires the progress nudge; a raising notifier
    # must not break the approval path - the run stays parked, ready for the
    # next approver.
    orch.approve(run_id, user="alice@example.com")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL


def test_no_notifier_is_a_silent_no_op(session_factory) -> None:
    orch = FoundryOrchestrator(session_factory)  # notifier defaults to None
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
