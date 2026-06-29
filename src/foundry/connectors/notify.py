"""Outbound run notifications (chat surfaces).

The tracker write-back (``IssueTracker``) mirrors a run *into the ticket*. This
is the parallel seam for the *chat* surface: it posts the interactive approval
message an approver clicks, and short status notifications as the run moves
through the notable points of its lifecycle (parked, blocked, PR open, merged).

Keeping this a Protocol means the orchestrator never imports Slack directly, and
tests use an in-memory fake. It is deliberately separate from ``IssueTracker``:
the approval message must carry buttons whose ``action_id``/``value`` the inbound
Slack interactivity parser (``api/slack.py``) already understands, closing the
post -> click -> ``_apply_decision`` loop, and notifications are best-effort (a
chat outage must never break a run, exactly like the tracker write-back).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from foundry.schemas.common import RunStatus

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalRequest:
    """The context a chat surface needs to render an actionable approval message.

    ``issue_id`` is the value the decision buttons carry (it is what the inbound
    parser maps back to a run), so the click round-trips to the same run. The
    rest is presentation: enough for an approver to decide without leaving chat.
    """

    issue_id: str
    issue_key: str | None
    title: str
    work_type: str
    risk: str
    agent_mode: str
    repo: str
    acceptance_criteria: tuple[str, ...] = ()
    required_approvals: tuple[str, ...] = ()
    # Effective N-of-M count: the number of *distinct* humans who must sign off
    # before the run advances (``policy.min_approvals`` raised by any per-repo
    # override, issue #31). Surfaced in the approval message only when >1, so the
    # default single-approval prompt is byte-for-byte unchanged.
    min_approvals: int = 1


@dataclass(frozen=True)
class ApprovalProgress:
    """A mid-flow nudge for the *next* approver of an N-of-M run (issue #31).

    The first approval prompt (``ApprovalRequest``) tells approvers up front how
    many *distinct* sign-offs a run needs. Once one lands but the run is still
    short, this is what re-pings the next approver - so a two-person-rule run
    doesn't go silent between sign-offs. ``collected``/``required`` are the
    distinct-approver count so far and the effective minimum; ``last_approver``
    is who just signed.
    """

    issue_id: str
    issue_key: str | None
    collected: int
    required: int
    last_approver: str

    @property
    def remaining(self) -> int:
        return max(0, self.required - self.collected)


class RunNotifier(Protocol):
    def approval_requested(self, request: ApprovalRequest) -> None: ...

    def status_changed(
        self, issue_id: str, issue_key: str | None, status: RunStatus
    ) -> None: ...

    def approval_progress(self, progress: ApprovalProgress) -> None: ...


class MultiNotifier:
    """Fan a run's notifications out to several chat surfaces (e.g. Slack + Teams).

    The orchestrator holds exactly one ``RunNotifier``; this lets more than one
    live surface receive every approval message / status update / progress nudge.
    Each child is isolated: a surface that raises is logged and skipped so it
    can never starve the others (the orchestrator already swallows a notifier
    failure, but that catch would stop a naive loop at the first raising child).
    """

    def __init__(self, notifiers: "list[RunNotifier]") -> None:
        self._notifiers = list(notifiers)

    def _fan_out(self, method: str, *args: object) -> None:
        for notifier in self._notifiers:
            try:
                getattr(notifier, method)(*args)
            except Exception:
                _log.exception("notifier %r failed on %s", notifier, method)

    def approval_requested(self, request: ApprovalRequest) -> None:
        self._fan_out("approval_requested", request)

    def status_changed(
        self, issue_id: str, issue_key: str | None, status: RunStatus
    ) -> None:
        self._fan_out("status_changed", issue_id, issue_key, status)

    def approval_progress(self, progress: ApprovalProgress) -> None:
        self._fan_out("approval_progress", progress)


@dataclass
class InMemoryNotifier:
    """Test double that records what would have been sent to the chat surface."""

    approvals: list[ApprovalRequest] = field(default_factory=list)
    statuses: list[tuple[str, str | None, RunStatus]] = field(default_factory=list)
    progress: list[ApprovalProgress] = field(default_factory=list)

    def approval_requested(self, request: ApprovalRequest) -> None:
        self.approvals.append(request)

    def status_changed(
        self, issue_id: str, issue_key: str | None, status: RunStatus
    ) -> None:
        self.statuses.append((issue_id, issue_key, status))

    def approval_progress(self, progress: ApprovalProgress) -> None:
        self.progress.append(progress)
