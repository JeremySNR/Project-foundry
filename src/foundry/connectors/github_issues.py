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

import hashlib
from typing import Any, Callable
from urllib.parse import quote

from foundry.schemas.ticket import RawTicket

# transport(method, path, body=None) -> parsed JSON (github_transport shape)
Transport = Callable[..., Any]

_STATE_LABEL_PREFIX = "foundry:status:"
_MAX_KEY_PREFIX = 9  # issue-key pattern allows a 10-char alnum prefix total
_HASH_LEN = 2  # trailing hash chars that disambiguate same-named repos


def github_issue_key(repo_full_name: str, number: int | str) -> str:
    """Deterministic ``REPONAME-123`` key for an issue.

    Built from the repo name (alnum only, upper-cased, truncated) plus a short
    deterministic hash of the *full* ``owner/repo`` path, so it matches the
    ``[A-Za-z][A-Za-z0-9]{1,9}-\\d+`` pattern the orchestrator extracts from
    branch names and PR titles when correlating delegated-agent PRs.

    The hash suffix is what keeps repos that normalise to the same prefix
    distinct: without it ``acme/my-app`` and ``acme/myapp`` (or ``acme/web`` and
    ``beta/web``) both became ``MYAPP``/``WEB`` and a PR could correlate to the
    wrong run. The hash derives from the whole ``owner/repo`` string so the
    owner disambiguates identically-named repos across orgs too.
    """
    name = repo_full_name.rsplit("/", 1)[-1]
    alnum = "".join(c for c in name if c.isalnum()).upper()
    digest = hashlib.sha1(repo_full_name.encode("utf-8")).hexdigest()[
        :_HASH_LEN
    ].upper()
    prefix = (alnum[: _MAX_KEY_PREFIX + 1 - _HASH_LEN] + digest)
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
        """Track Foundry's pipeline position as a ``foundry:status:...`` label.

        Adds the new status label and removes any *stale* status labels rather
        than rewriting the whole set with a single PUT. A PUT GETs-then-PUTs the
        full label list, so a concurrent label edit between the two calls is
        silently clobbered; additive ``POST`` + targeted ``DELETE`` only ever
        touches Foundry's own ``foundry:status:`` labels, leaving everything
        else alone.
        """
        repo, number = split_issue_id(issue_id)
        slug = state_name.lower().replace("foundry:", "").strip().replace(" ", "-")
        new_label = f"{_STATE_LABEL_PREFIX}{slug}"
        # Add first so the issue is never momentarily missing a status label.
        self._transport(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            {"labels": [new_label]},
        )
        data = self._transport("GET", f"/repos/{repo}/issues/{number}")
        current = [
            lab["name"] if isinstance(lab, dict) else str(lab)
            for lab in data.get("labels") or []
        ]
        for lab in current:
            if lab.startswith(_STATE_LABEL_PREFIX) and lab != new_label:
                self._transport(
                    "DELETE",
                    f"/repos/{repo}/issues/{number}/labels/{quote(lab, safe='')}",
                )
