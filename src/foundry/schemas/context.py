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


class CodeOwnersRule(BaseModel):
    """One CODEOWNERS line: a path pattern and the owners it assigns."""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    owners: list[str] = Field(default_factory=list)


class ManifestFacts(BaseModel):
    """Facts extracted from one dependency manifest (pyproject, package.json...)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str  # "pyproject" | "package_json" | "go_mod" | "cargo" | ...
    dependencies: list[str] = Field(default_factory=list)  # capped at sync time
    test_command: str | None = None


class RepoCodeFacts(BaseModel):
    """Code-level facts for one repository, gathered by the catalog sync.

    Derived from the SCM tree API plus targeted content fetches (CODEOWNERS and
    root manifests only) - never a clone, never arbitrary file contents.
    """

    model_config = ConfigDict(extra="forbid")

    repo: str
    default_branch: str | None = None
    test_layout: list[str] = Field(default_factory=list)  # e.g. ["tests/", "*_test.go"]
    codeowners: list[CodeOwnersRule] = Field(default_factory=list)
    manifests: list[ManifestFacts] = Field(default_factory=list)
    languages: dict[str, int] = Field(default_factory=dict)  # extension -> file count
    conventions: list[str] = Field(default_factory=list)  # "GitHub Actions CI", ...
    tree_truncated: bool = False


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_repositories: list[CandidateRepository] = Field(default_factory=list)
    candidate_files: list[CandidateFile] = Field(default_factory=list)
    related_prs: list[str] = Field(default_factory=list)
    related_issues: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    # Code-level facts for threshold-clearing candidates (usually exactly one).
    # Defaulted so bundles persisted before this field existed still validate.
    code_facts: list[RepoCodeFacts] = Field(default_factory=list)

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
