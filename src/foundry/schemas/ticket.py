"""RawTicket - the immutable snapshot of a Linear issue at intake.

This is the input to the intelligence engines. It is stored verbatim as the
``ticket_snapshot`` artifact so every downstream decision can be traced back to
exactly what Foundry saw.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LinkedResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str  # e.g. "github_pr", "github_issue", "repo"
    url: str
    repo: str | None = None


class RawTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str
    issue_key: str
    title: str
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    comments: list[str] = Field(default_factory=list)
    linked_resources: list[LinkedResource] = Field(default_factory=list)
    # Repositories the team has explicitly associated with the issue, if any.
    known_repositories: list[str] = Field(default_factory=list)

    def text_blob(self) -> str:
        """All free-text on the ticket, lower-cased, for keyword heuristics."""
        parts = [self.title, self.description, *self.comments]
        return "\n".join(parts).lower()
