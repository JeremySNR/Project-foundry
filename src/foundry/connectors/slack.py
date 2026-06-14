"""Outbound Slack notifications: the approval message and run status updates.

The mirror image of ``api/slack.py`` (which parses the *inbound* button click).
This module *posts* the message the approver clicks and short status updates as a
run moves through its notable lifecycle points. It is a ``RunNotifier``
(``connectors/notify.py``) wired behind an injected ``transport`` so it stays
testable - tests pass a fake that records payloads, the live path is
``slack_transport`` (``connectors/transport.py``) talking to ``chat.postMessage``.

The approval message's buttons are the load-bearing contract: their ``action_id``
is ``foundry_{approve,reject,stop}`` and their ``value`` is the issue id, exactly
what ``parse_slack_interaction`` expects, so a click round-trips through the same
policy-gated ``_apply_decision`` path as every other approval surface.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.connectors.notify import ApprovalRequest
from foundry.schemas.common import RunStatus

# The button wire contract shared with the inbound parser (``api/slack.py``):
# the outbound message emits these action_ids, the parser strips the prefix back
# off. Defined here (the renderer that emits them) so the dependency flows
# api -> connectors, not the other way round.
SLACK_ACTION_PREFIX = "foundry_"
SLACK_DECISIONS = frozenset({"approve", "reject", "stop"})

# Buttons the approval message offers, paired with Slack's button "style" so the
# destructive verbs read as such. action_id = prefix + command (the inbound
# parser strips the prefix back off); value = issue id.
_APPROVAL_BUTTONS: tuple[tuple[str, str, str | None], ...] = (
    ("approve", "Approve", "primary"),
    ("reject", "Reject", "danger"),
    ("stop", "Stop", None),
)

# Human-facing labels for the statuses worth pinging chat about. Only these are
# notified (the orchestrator filters); the rest are routine intermediate states.
_STATUS_LABELS: dict[RunStatus, str] = {
    RunStatus.NEEDS_CLARIFICATION: ":warning: Parked - needs clarification",
    RunStatus.BLOCKED: ":no_entry: Blocked",
    RunStatus.REJECTED: ":no_entry: Rejected",
    RunStatus.EXECUTION_FAILED: ":no_entry: Execution failed",
    RunStatus.PR_OPEN: ":git: PR open",
    RunStatus.COMPLETE: ":white_check_mark: Merged - run complete",
}


def status_label(status: RunStatus) -> str:
    return _STATUS_LABELS.get(
        status, status.value.replace("_", " ").title()
    )


class SlackNotifier:
    """Posts approval messages and status updates to a Slack channel.

    ``transport(text, blocks) -> response`` does the channel-scoped
    ``chat.postMessage``; ``text`` is the notification fallback and ``blocks`` is
    the rich Block Kit body.
    """

    def __init__(self, transport: Callable[[str, list[dict[str, Any]]], Any]) -> None:
        self._transport = transport

    def approval_requested(self, request: ApprovalRequest) -> None:
        self._transport(*_approval_message(request))

    def status_changed(
        self, issue_id: str, issue_key: str | None, status: RunStatus
    ) -> None:
        self._transport(*_status_message(issue_id, issue_key, status))


def _ref(issue_id: str, issue_key: str | None) -> str:
    """A human reference to the issue: the key when we have it, else the id."""
    return issue_key or issue_id


def _approval_message(req: ApprovalRequest) -> tuple[str, list[dict[str, Any]]]:
    ref = _ref(req.issue_id, req.issue_key)
    text = f"Run awaiting approval: {req.title} ({ref})"
    fields = [
        f"*Issue:*\n{ref}",
        f"*Work type:*\n`{req.work_type}`",
        f"*Risk:*\n`{req.risk}`",
        f"*Mode:*\n`{req.agent_mode}`",
        f"*Repo:*\n`{req.repo}`",
    ]
    if req.required_approvals:
        fields.append("*Required approval:*\n" + ", ".join(req.required_approvals))
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Foundry: approval needed"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{req.title}*"}},
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f} for f in fields],
        },
    ]
    if req.acceptance_criteria:
        ac = "\n".join(f"- {c}" for c in req.acceptance_criteria)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Acceptance criteria*\n{ac}"},
            }
        )
    blocks.append(
        {
            "type": "actions",
            "block_id": "foundry_decision",
            "elements": [
                _button(command, label, style, req.issue_id)
                for command, label, style in _APPROVAL_BUTTONS
            ],
        }
    )
    return text, blocks


def _button(command: str, label: str, style: str | None, issue_id: str) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "button",
        "action_id": f"{SLACK_ACTION_PREFIX}{command}",
        "text": {"type": "plain_text", "text": label},
        "value": issue_id,
    }
    if style is not None:
        element["style"] = style
    return element


def _status_message(
    issue_id: str, issue_key: str | None, status: RunStatus
) -> tuple[str, list[dict[str, Any]]]:
    ref = _ref(issue_id, issue_key)
    label = status_label(status)
    text = f"Foundry run {ref}: {label}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    return text, blocks
