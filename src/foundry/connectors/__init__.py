"""Connector layer - adapters for the tools Foundry coordinates."""

from __future__ import annotations

from .base import InMemoryIssueTracker, IssueTracker
from .comments import (
    format_analysis_comment,
    format_cursor_delegation,
    state_for,
)
from .github import GitHubConnector
from .github_issues import GitHubIssuesConnector
from .gitlab import GitLabConnector
from .jira import JiraConnector
from .linear import LinearConnector

__all__ = [
    "IssueTracker",
    "InMemoryIssueTracker",
    "LinearConnector",
    "GitHubConnector",
    "GitHubIssuesConnector",
    "GitLabConnector",
    "JiraConnector",
    "format_analysis_comment",
    "format_cursor_delegation",
    "state_for",
]
