"""GitLab as the SCM.

Maps GitLab webhook events to the same :class:`PullRequestState` the
orchestrator already consumes for GitHub PRs - a merge request is a pull
request as far as the governed loop is concerned. Supported hooks:

- ``Merge Request Hook`` - the primary signal (open / update / approve /
  merge / close).
- ``Pipeline Hook``      - CI status, when the pipeline is attached to an MR.

GitLab webhooks authenticate with a shared token sent verbatim in
``X-Gitlab-Token`` (no HMAC); the endpoint compares it in constant time.
"""

from __future__ import annotations

from typing import Any

from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus
from foundry.schemas.pr import PullRequestState

_MR_STATE = {
    "opened": PRStatus.OPEN,
    "reopened": PRStatus.OPEN,
    "locked": PRStatus.OPEN,
    "merged": PRStatus.MERGED,
    "closed": PRStatus.CLOSED,
}

_PIPELINE_STATUS = {
    "success": CIStatus.PASSING,
    "failed": CIStatus.FAILING,
    "canceled": CIStatus.FAILING,
    "running": CIStatus.PENDING,
    "pending": CIStatus.PENDING,
    "created": CIStatus.PENDING,
}


class GitLabConnector:
    def pr_state_from_event(
        self, event: str, payload: dict[str, Any]
    ) -> PullRequestState | None:
        """Build a PullRequestState from a webhook payload, or None if irrelevant."""
        if event == "Merge Request Hook":
            return self._from_merge_request(payload)
        if event == "Pipeline Hook":
            return self._from_pipeline(payload)
        return None

    def _from_merge_request(self, payload: dict[str, Any]) -> PullRequestState | None:
        attrs = payload.get("object_attributes") or {}
        repo = (payload.get("project") or {}).get("path_with_namespace")
        if not attrs or not repo or attrs.get("iid") is None:
            return None
        status = _MR_STATE.get(attrs.get("state") or "", PRStatus.OPEN)
        if status is PRStatus.OPEN and (
            attrs.get("draft") or attrs.get("work_in_progress")
        ):
            status = PRStatus.DRAFT
        state = PullRequestState(
            repo=repo,
            pr_number=attrs["iid"],
            url=attrs.get("url") or "",
            branch=attrs.get("source_branch") or "",
            title=attrs.get("title") or "",
            status=status,
        )
        if attrs.get("action") == "approved":
            state.review_status = ReviewStatus.APPROVED
        return state

    def _from_pipeline(self, payload: dict[str, Any]) -> PullRequestState | None:
        attrs = payload.get("object_attributes") or {}
        mr = payload.get("merge_request") or {}
        repo = (payload.get("project") or {}).get("path_with_namespace")
        # Only pipelines attached to a merge request close the loop.
        if not mr or not repo or mr.get("iid") is None:
            return None
        return PullRequestState(
            repo=repo,
            pr_number=mr["iid"],
            url=mr.get("url") or "",
            branch=mr.get("source_branch") or attrs.get("ref") or "",
            title=mr.get("title") or "",
            status=_MR_STATE.get(mr.get("state") or "", PRStatus.OPEN),
            ci_status=_PIPELINE_STATUS.get(attrs.get("status") or "", CIStatus.UNKNOWN),
        )
