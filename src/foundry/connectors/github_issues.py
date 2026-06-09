"""GitHub Issues as the issue tracker.

For teams that plan in GitHub instead of Linear: the issue *is* the ticket.
Implements the same ``IssueTracker`` protocol the orchestrator already speaks,
so nothing upstream changes - trigger with the ``foundry:candidate`` label or
a ``/foundry analyse`` comment, approve with ``/foundry approve``.

Identifiers:

- ``issue_id``  is ``owner/repo#number`` (globally unique, webhook-derivable).
- ``issue_key`` is a synthesised ``REPONAME-number`` that matches the key
  pattern PR correlation already scans branch names and PR titles for, so the
  delegated-agent loop closes exactly like it does for Linear keys.

GitHub issues have no workflow states, only open/closed - so ``set_state``
maps Foundry's status to a ``foundry:...`` label (replacing the previous one),
which is the idiomatic GitHub way to track pipeline position.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.schemas.ticket import RawTicket

# transport(method, path, body=None) -> parsed JSON (github_transport shape)
Transport = Callable[..., Any]

_STATE_LABEL_PREFIX = "foundry:status:"
_MAX_KEY_PREFIX = 9  # issue-key pattern allows a 10-char alnum prefix total


def github_issue_key(repo_full_name: str, number: int | str) -> str:
    """Deterministic ``REPONAME-123`` key for an issue.

    Built from the repo name (alnum only, upper-cased, truncated) so it matches
    the ``[A-Za-z][A-Za-z0-9]{1,9}-\\d+`` pattern the orchestrator extracts
    from branch names and PR titles when correlating delegated-agent PRs.
    """
    name = repo_full_name.rsplit("/", 1)[-1]
    prefix = "".join(c for c in name if c.isalnum()).upper()[: _MAX_KEY_PREFIX + 1]
    if not prefix or not prefix[0].isalpha():
        prefix = ("X" + prefix)[: _MAX_KEY_PREFIX + 1]
    if len(prefix) < 2:  # the key pattern needs a 2+ character prefix
        prefix = (prefix + "XX")[:2]
    return f"{prefix}-{number}"


def split_issue_id(issue_id: str) -> tuple[str, str]:
    """``owner/repo#123`` -> (``owner/repo``, ``123``)."""
    repo, _, number = issue_id.rpartition("#")
    if not repo or not number.isdigit():
        raise ValueError(f"expected 'owner/repo#number', got {issue_id!r}")
    return repo, number


class GitHubIssuesConnector:
    """IssueTracker over the GitHub Issues REST API."""

    def __init__(self, *, transport: Transport) -> None:
        self._transport = transport

    def get_issue(self, issue_id: str) -> RawTicket:
        repo, number = split_issue_id(issue_id)
        data = self._transport("GET", f"/repos/{repo}/issues/{number}")
        labels = [
            lab["name"] if isinstance(lab, dict) else str(lab)
            for lab in data.get("labels") or []
        ]
        # Explicit repo: labels override the host repo; never both, because two
        # confident candidates reads as ambiguity and parks the run.
        labelled = [lab[len("repo:"):] for lab in labels if lab.startswith("repo:")]
        return RawTicket(
            issue_id=issue_id,
            issue_key=github_issue_key(repo, number),
            title=data.get("title") or "",
            description=data.get("body") or "",
            labels=labels,
            known_repositories=labelled or [repo],
        )

    def post_comment(self, issue_id: str, body: str) -> None:
        repo, number = split_issue_id(issue_id)
        self._transport(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}
        )

    def set_state(self, issue_id: str, state_name: str) -> None:
        """Track Foundry's pipeline position as a ``foundry:status:...`` label."""
        repo, number = split_issue_id(issue_id)
        data = self._transport("GET", f"/repos/{repo}/issues/{number}")
        kept = [
            lab["name"] if isinstance(lab, dict) else str(lab)
            for lab in data.get("labels") or []
        ]
        kept = [lab for lab in kept if not lab.startswith(_STATE_LABEL_PREFIX)]
        slug = state_name.lower().replace("foundry:", "").strip().replace(" ", "-")
        self._transport(
            "PUT",
            f"/repos/{repo}/issues/{number}/labels",
            {"labels": kept + [f"{_STATE_LABEL_PREFIX}{slug}"]},
        )
