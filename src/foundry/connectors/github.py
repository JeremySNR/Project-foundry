"""GitHub connector.

Foundry observes the PR that a coding agent (Cursor, via Linear) opens - it does
not assume the agent succeeded. This adapter maps GitHub webhook events to a
:class:`PullRequestState`, optionally enriching the changed-file list via the
REST API (injected ``transport`` - no network in tests).

Supported events:

- ``pull_request``        - the primary signal (opened / synchronize / closed).
- ``pull_request_review`` - sets review status (a bot login => bot-reviewed,
  matching CodeRabbit-style reviewers).
- ``check_suite``         - sets CI status.

The changed-file list matters because the policy gate blocks forbidden paths and
flags oversized PRs; when no transport is configured the list is empty and only
the branch/status mapping applies.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus
from foundry.schemas.pr import PullRequestState

# transport(method, path) -> parsed JSON
Transport = Callable[[str, str], Any]

_CHECK_CONCLUSION = {
    "success": CIStatus.PASSING,
    "failure": CIStatus.FAILING,
    "timed_out": CIStatus.FAILING,
    "cancelled": CIStatus.FAILING,
}

_REVIEW_STATE = {
    "approved": ReviewStatus.APPROVED,
    "changes_requested": ReviewStatus.CHANGES_REQUESTED,
}

# GitHub defaults to 30 items per page; the file list feeds the forbidden-path
# hard block, so truncation would let files beyond the first page bypass the
# gate. Page at the API maximum and loop until a short page.
_PER_PAGE = 100


class GitHubConnector:
    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = transport

    def list_pr_files(self, repo: str, number: int) -> list[str]:
        if self._transport is None:
            return []
        files: list[str] = []
        page = 1
        while True:
            data = self._transport(
                "GET",
                f"/repos/{repo}/pulls/{number}/files"
                f"?per_page={_PER_PAGE}&page={page}",
            )
            batch = data or []
            files.extend(f["filename"] for f in batch if f.get("filename"))
            if len(batch) < _PER_PAGE:
                return files
            page += 1

    def failing_check_summaries(self, repo: str, suite_id: int) -> str:
        """Names + output summaries of failed check runs, for remediation context.

        Empty string when no transport is configured or nothing failed - the
        feedback loop still works, the agent just gets less detail.
        """
        if self._transport is None or not suite_id:
            return ""
        lines: list[str] = []
        page = 1
        while True:
            data = self._transport(
                "GET",
                f"/repos/{repo}/check-suites/{suite_id}/check-runs"
                f"?per_page={_PER_PAGE}&page={page}",
            )
            batch = (data or {}).get("check_runs") or []
            for check in batch:
                if (check.get("conclusion") or "").lower() not in {
                    "failure",
                    "timed_out",
                    "cancelled",
                }:
                    continue
                name = check.get("name", "unnamed check")
                output = check.get("output") or {}
                summary = (output.get("summary") or output.get("title") or "").strip()
                lines.append(f"- {name}: {summary}" if summary else f"- {name}")
            if len(batch) < _PER_PAGE:
                return "\n".join(lines)
            page += 1

    def pr_state_from_event(
        self, event: str, payload: dict[str, Any]
    ) -> PullRequestState | None:
        """Build a PullRequestState from a webhook payload, or None if irrelevant."""
        if event == "pull_request":
            return self._from_pull_request(payload)
        if event == "pull_request_review":
            return self._from_review(payload)
        if event == "check_suite":
            return self._from_check_suite(payload)
        return None

    # -- per-event mapping ----------------------------------------------------

    def _base(self, pr: dict[str, Any], repo: str) -> PullRequestState:
        return PullRequestState(
            repo=repo,
            pr_number=pr["number"],
            url=pr.get("html_url", ""),
            branch=pr.get("head", {}).get("ref", ""),
            title=pr.get("title", "") or "",
            status=self._status(pr),
        )

    def _from_pull_request(self, payload: dict[str, Any]) -> PullRequestState | None:
        pr = payload.get("pull_request")
        repo = (payload.get("repository") or {}).get("full_name")
        if not pr or not repo:
            return None
        state = self._base(pr, repo)
        state.files_changed = self.list_pr_files(repo, pr["number"])
        return state

    def _from_review(self, payload: dict[str, Any]) -> PullRequestState | None:
        # Only a freshly *submitted* review reports a completed review. A
        # ``dismissed`` (or ``edited``) action carries a review object whose
        # state would otherwise fall through to HUMAN_REVIEWED/BOT_REVIEWED -
        # reading a withdrawn review as a real one. GitHub always sends an
        # action for this event; a missing one is treated leniently.
        action = (payload.get("action") or "").lower()
        if action and action != "submitted":
            return None
        pr = payload.get("pull_request")
        review = payload.get("review") or {}
        repo = (payload.get("repository") or {}).get("full_name")
        if not pr or not repo:
            return None
        state = self._base(pr, repo)
        is_bot = (review.get("user") or {}).get("type") == "Bot"
        state.review_status = _REVIEW_STATE.get(
            (review.get("state") or "").lower(),
            ReviewStatus.BOT_REVIEWED if is_bot else ReviewStatus.HUMAN_REVIEWED,
        )
        return state

    def _from_check_suite(self, payload: dict[str, Any]) -> PullRequestState | None:
        suite = payload.get("check_suite") or {}
        repo = (payload.get("repository") or {}).get("full_name")
        if not repo:
            return None
        # Pick the PR whose head matches this suite. Fork PRs arrive with an
        # empty ``pull_requests`` list (GitHub omits PRs living in another repo),
        # so fall back to the suite's own ``head_branch`` - correlation is by
        # branch/issue-key, not PR number, and dropping the event would lose CI
        # status for every fork contribution.
        head_branch = suite.get("head_branch") or ""
        prs = suite.get("pull_requests") or []
        pr = next(
            (p for p in prs if (p.get("head") or {}).get("ref") == head_branch),
            prs[0] if prs else None,
        )
        if pr is None and not head_branch:
            return None
        state = PullRequestState(
            repo=repo,
            pr_number=(pr or {}).get("number", 0),
            url=(pr or {}).get("url", ""),
            branch=((pr or {}).get("head") or {}).get("ref", "") or head_branch,
            status=PRStatus.OPEN,
        )
        conclusion = (suite.get("conclusion") or "").lower()
        state.ci_status = _CHECK_CONCLUSION.get(conclusion, CIStatus.PENDING)
        if state.ci_status is CIStatus.FAILING:
            # Attach failing check details so a remediation dispatch can tell
            # the agent *what* failed, not just that something did.
            state.summary = self.failing_check_summaries(repo, suite.get("id") or 0)
        return state

    @staticmethod
    def _status(pr: dict[str, Any]) -> PRStatus:
        if pr.get("merged"):
            return PRStatus.MERGED
        if pr.get("state") == "closed":
            return PRStatus.CLOSED
        if pr.get("draft"):
            return PRStatus.DRAFT
        return PRStatus.OPEN
