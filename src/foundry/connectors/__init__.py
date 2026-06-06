"""Connector layer - adapters for the tools Foundry coordinates."""

from __future__ import annotations

from .base import InMemoryIssueTracker, IssueTracker
from .comments import (
    format_analysis_comment,
    format_cursor_delegation,
    state_for,
)
from .linear import LinearConnector

__all__ = [
    "IssueTracker",
    "InMemoryIssueTracker",
    "LinearConnector",
    "format_analysis_comment",
    "format_cursor_delegation",
    "state_for",
]
