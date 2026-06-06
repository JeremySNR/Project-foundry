"""ContextBundle - evidence gathered about where work should happen.

For the MVP, context is sourced from GitHub only (Datadog etc. come later).
The core rule: never assume the repo from the title alone; attach a confidence
to every candidate and refuse to choose when confidence is low.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import REPO_CONFIDENCE_THRESHOLD


class CandidateRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    confidence: int = Field(ge=0, le=100)
    reason: str


class CandidateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_repositories: list[CandidateRepository] = Field(default_factory=list)
    candidate_files: list[CandidateFile] = Field(default_factory=list)
    related_prs: list[str] = Field(default_factory=list)
    related_issues: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    @property
    def best_repository(self) -> CandidateRepository | None:
        """Highest-confidence repository candidate, if any."""
        if not self.candidate_repositories:
            return None
        return max(self.candidate_repositories, key=lambda r: r.confidence)

    def has_confident_repository(
        self, threshold: int = REPO_CONFIDENCE_THRESHOLD
    ) -> bool:
        """True when exactly one repo clears the confidence threshold.

        Multiple plausible repos above threshold is ambiguous and should route
        to human confirmation rather than autonomous execution.
        """
        confident = [
            r for r in self.candidate_repositories if r.confidence >= threshold
        ]
        return len(confident) == 1
