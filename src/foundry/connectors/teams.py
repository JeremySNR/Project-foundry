"""Outbound Microsoft Teams notifications: the approval message and status updates.

The mirror image of ``api/teams.py`` (which parses the *inbound* command the
approver types). This module *posts* the Adaptive Card the approver reads and
short status updates as a run moves through its notable lifecycle points. It is a
``RunNotifier`` (``connectors/notify.py``) wired behind an injected ``transport``
so it stays testable - tests pass a fake that records payloads, the live path is
``teams_transport`` (``connectors/transport.py``) posting to a Teams Incoming
Webhook.

Unlike Slack (whose buttons POST a signed interaction straight back), the Teams
twin uses an Outgoing Webhook for the inbound half: the approver @mentions the
bot and *types* the command. So the card carries the exact command syntax -
``approve <issue-id>`` - where ``<issue-id>`` is the value the inbound parser
(``api/teams.py``) maps back to a run, closing the same
post -> reply -> ``_apply_decision`` loop as every other approval surface.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.connectors.notify import ApprovalProgress, ApprovalRequest
from foundry.schemas.common import RunStatus

# The verb vocabulary shared with the inbound parser (``api/teams.py``): the card
# prints these as the command syntax, the parser accepts exactly them. Defined
# here (the renderer that prints them) so the dependency flows api -> connectors,
# not the other way round - the same layering Slack uses.
TEAMS_DECISIONS = frozenset({"approve", "reject", "stop"})
# Optional leading token the inbound parser tolerates (the tracker-comment form),
# kept here next to the verbs so the contract lives in one place.
TEAMS_COMMAND_PREFIX = "/foundry"

# Human-facing labels for the statuses worth pinging chat about, in plain text
# (Teams does not render Slack's ``:emoji:`` shortcodes). Only these are notified
# (the orchestrator filters); the rest are routine intermediate states.
_STATUS_LABELS: dict[RunStatus, str] = {
    RunStatus.NEEDS_CLARIFICATION: "⚠️ Parked - needs clarification",
    RunStatus.BLOCKED: "⛔ Blocked",
    RunStatus.REJECTED: "⛔ Rejected",
    RunStatus.EXECUTION_FAILED: "⛔ Execution failed",
    RunStatus.PR_OPEN: "🔧 PR open",
    RunStatus.COMPLETE: "✅ Merged - run complete",
}


def status_label(status: RunStatus) -> str:
    return _STATUS_LABELS.get(status, status.value.replace("_", " ").title())


class TeamsNotifier:
    """Posts approval messages and status updates to a Teams channel.

    ``transport(text, card) -> response`` posts the Adaptive Card to the
    configured Incoming Webhook; ``text`` is a plain-text fallback/summary and
    ``card`` is the Adaptive Card body.
    """

    def __init__(self, transport: Callable[[str, dict[str, Any]], Any]) -> None:
        self._transport = transport

    def approval_requested(self, request: ApprovalRequest) -> None:
        self._transport(*_approval_message(request))

    def status_changed(
        self, issue_id: str, issue_key: str | None, status: RunStatus
    ) -> None:
        self._transport(*_status_message(issue_id, issue_key, status))

    def approval_progress(self, progress: ApprovalProgress) -> None:
        self._transport(*_approval_progress_message(progress))


def _ref(issue_id: str, issue_key: str | None) -> str:
    """A human reference to the issue: the key when we have it, else the id."""
    return issue_key or issue_id


def _text_block(text: str, **extra: Any) -> dict[str, Any]:
    block = {"type": "TextBlock", "text": text, "wrap": True}
    block.update(extra)
    return block


def _card(body: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }


def _command_hint(issue_id: str) -> dict[str, Any]:
    """The line telling the approver exactly what to send the bot.

    The command carries ``issue_id`` (not the human key): it is what the inbound
    parser maps back to a run, exactly as the Slack button ``value`` does.
    """
    return _text_block(
        f"To decide, @mention this bot and reply: `approve {issue_id}` "
        f"(or `reject {issue_id}` / `stop {issue_id}`).",
        isSubtle=True,
        spacing="Medium",
    )


def _approval_message(req: ApprovalRequest) -> tuple[str, dict[str, Any]]:
    ref = _ref(req.issue_id, req.issue_key)
    text = f"Run awaiting approval: {req.title} ({ref})"
    facts = [
        {"title": "Issue", "value": ref},
        {"title": "Work type", "value": req.work_type},
        {"title": "Risk", "value": req.risk},
        {"title": "Mode", "value": req.agent_mode},
        {"title": "Repo", "value": req.repo},
    ]
    if req.required_approvals:
        facts.append(
            {"title": "Required approval", "value": ", ".join(req.required_approvals)}
        )
    if req.min_approvals > 1:
        facts.append(
            {
                "title": "Approvers required",
                "value": f"{req.min_approvals} distinct sign-offs",
            }
        )
    body: list[dict[str, Any]] = [
        _text_block("Foundry: approval needed", weight="Bolder", size="Large"),
        _text_block(req.title, weight="Bolder"),
        {"type": "FactSet", "facts": facts},
    ]
    if req.acceptance_criteria:
        ac = "\n".join(f"- {c}" for c in req.acceptance_criteria)
        body.append(_text_block("Acceptance criteria", weight="Bolder"))
        body.append(_text_block(ac))
    body.append(_command_hint(req.issue_id))
    return text, _card(body)


def _status_message(
    issue_id: str, issue_key: str | None, status: RunStatus
) -> tuple[str, dict[str, Any]]:
    ref = _ref(issue_id, issue_key)
    label = status_label(status)
    text = f"Foundry run {ref}: {label}"
    return text, _card([_text_block(text)])


def _approval_progress_message(
    progress: ApprovalProgress,
) -> tuple[str, dict[str, Any]]:
    """The mid-flow nudge for the next approver of an N-of-M run (issue #31).

    A short, non-interactive progress message - the original approval card (with
    its command hint) is already in the channel; this just tells the next
    approver that one sign-off has landed and another is needed.
    """
    ref = _ref(progress.issue_id, progress.issue_key)
    remaining = progress.remaining
    plural = "s" if remaining != 1 else ""
    text = (
        f"⏳ Foundry run {ref}: "
        f"{progress.collected} of {progress.required} approvals collected "
        f"(latest: {progress.last_approver}) - "
        f"{remaining} more distinct sign-off{plural} needed to proceed"
    )
    return text, _card([_text_block(text)])
