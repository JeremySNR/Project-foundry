"""PullRequestState - Foundry's observed view of a GitHub PR.

Foundry never assumes the agent succeeded; it monitors the PR independently.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import CIStatus, OverallRisk, PRStatus, ReviewStatus


class PullRequestState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int
    url: str
    branch: str
    # PR title, used to correlate delegated-agent PRs back to a run via the
    # embedded issue key when the branch name was not chosen by Foundry.
    title: str = ""
    status: PRStatus
    ci_status: CIStatus = CIStatus.UNKNOWN
    review_status: ReviewStatus = ReviewStatus.NONE
    files_changed: list[str] = Field(default_factory=list)
    risk_delta: OverallRisk = OverallRisk.LOW
    summary: str = ""
