"""GitLab as the SCM.

Maps GitLab webhook events to the same :class:`PullRequestState` the
orchestrator already consumes for GitHub PRs - a merge request is a pull
request as far as the governed loop is concerned. Supported hooks:

- ``Merge Request Hook`` - the primary signal (open / update / approve /
  merge / close).
- ``Pipeline Hook``      - CI status, when the pipeline is attached to an MR.

GitLab webhooks authenticate with a shared token sent verbatim in
``X-Gitlab-Token`` (no HMAC); the endpoint compares it in constant time.

Like the GitHub connector, the changed-file list is enriched via the REST API
(injected ``transport`` - no network in tests). That list feeds the
forbidden-path hard block and the oversize/sensitive-area gates; without a
transport the list is empty and a GitLab MR runs diff-blind, so configure a
``gitlab_api_token`` in production to get the same gates GitHub PRs get.
"""

from __future__ import annotations

from urllib.parse import quote
from typing import Any, Callable

from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus
from foundry.schemas.pr import PullRequestState

# transport(method, path) -> parsed JSON
Transport = Callable[[str, str], Any]

# GitLab paginates the diffs endpoint; the file list feeds the forbidden-path
# hard block, so a truncated list would let files beyond the first page bypass
# the gate. Page at the API maximum and loop until a short page.
_PER_PAGE = 100

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
    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = transport

    def list_mr_files(self, repo: str, iid: int) -> list[str]:
        """Paths touched by a merge request, for the file-based safety gates.

        Returns an empty list when no transport is configured. Both the new and
        the old path are collected (deduped, order-preserving) so a rename *out
        of* a forbidden directory is still caught - strictly more conservative
        than reporting only the new path.
        """
        if self._transport is None:
            return []
        project = quote(repo, safe="")
        files: list[str] = []
        seen: set[str] = set()
        page = 1
        while True:
            data = self._transport(
                "GET",
                f"/projects/{project}/merge_requests/{iid}/diffs"
                f"?per_page={_PER_PAGE}&page={page}",
            )
            batch = data or []
            for diff in batch:
                for path in (diff.get("new_path"), diff.get("old_path")):
                    if path and path not in seen:
                        seen.add(path)
                        files.append(path)
            if len(batch) < _PER_PAGE:
                return files
            page += 1

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
        state.files_changed = self.list_mr_files(repo, attrs["iid"])
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
