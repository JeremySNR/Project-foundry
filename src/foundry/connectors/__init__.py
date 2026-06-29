"""Connector layer - adapters for the tools Foundry coordinates."""

from __future__ import annotations

from .base import InMemoryIssueTracker, IssueTracker
from .comments import (
    format_analysis_comment,
    format_approval_progress_comment,
    format_cursor_delegation,
    state_for,
)
from .github import GitHubConnector
from .github_issues import GitHubIssuesConnector
from .gitlab import GitLabConnector
from .jira import JiraConnector
from .linear import LinearConnector, LinearWriteError
from .notify import (
    ApprovalProgress,
    ApprovalRequest,
    InMemoryNotifier,
    MultiNotifier,
    RunNotifier,
)
from .slack import SlackNotifier, status_label
from .teams import TeamsNotifier

__all__ = [
    "IssueTracker",
    "InMemoryIssueTracker",
    "LinearConnector",
    "LinearWriteError",
    "GitHubConnector",
    "GitHubIssuesConnector",
    "GitLabConnector",
    "JiraConnector",
    "RunNotifier",
    "InMemoryNotifier",
    "MultiNotifier",
    "ApprovalRequest",
    "ApprovalProgress",
    "SlackNotifier",
    "TeamsNotifier",
    "status_label",
    "format_analysis_comment",
    "format_approval_progress_comment",
    "format_cursor_delegation",
    "state_for",
]
