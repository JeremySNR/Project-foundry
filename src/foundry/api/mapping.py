"""Map a Linear webhook payload to a :class:`RawTicket`.

This is an *interim* mapping for Track 1: it reads what a webhook carries
directly. Track 2's Linear connector will replace it with an authenticated fetch
of the full issue (and real linked GitHub resources / repo context).

Until then, the affected repository can be signalled with a Linear label of the
form ``repo:<name>`` so a run can reach a confident repository without a live
GitHub lookup.
"""

from __future__ import annotations

from typing import Any

from foundry.schemas.ticket import LinkedResource, RawTicket

_REPO_LABEL_PREFIX = "repo:"


def _coalesce(*values: Any) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def linear_payload_to_ticket(payload: dict[str, Any]) -> RawTicket:
    data = payload.get("data", {}) or {}
    # Comment events nest the issue; merge so title/description are found either way.
    issue = data.get("issue", {}) or {}

    issue_id = _coalesce(data.get("issueId"), data.get("id"), issue.get("id"))
    issue_key = _coalesce(
        data.get("identifier"), data.get("issueKey"), issue.get("identifier")
    )
    title = _coalesce(data.get("title"), issue.get("title"))
    description = _coalesce(
        data.get("description"), issue.get("description"), data.get("body")
    )

    label_objs = data.get("labels") or issue.get("labels") or []
    labels = [
        lab["name"]
        for lab in label_objs
        if isinstance(lab, dict) and lab.get("name")
    ]
    known_repositories = [
        lab[len(_REPO_LABEL_PREFIX) :]
        for lab in labels
        if lab.startswith(_REPO_LABEL_PREFIX)
    ]

    linked_resources = [
        LinkedResource(**{k: v for k, v in link.items() if k in {"kind", "url", "repo"}})
        for link in (data.get("linked_resources") or [])
        if isinstance(link, dict) and link.get("kind") and link.get("url")
    ]

    return RawTicket(
        issue_id=issue_id,
        issue_key=issue_key,
        title=title,
        description=description,
        labels=labels,
        known_repositories=known_repositories,
        linked_resources=linked_resources,
    )


def jira_payload_to_ticket(payload: dict[str, Any]) -> RawTicket:
    """Map a Jira webhook payload (issue or comment event) to a ticket.

    Jira keys (``ACME-42``) already match the pattern PR correlation scans
    branch names and titles for, so they pass through unchanged. Jira labels
    are plain strings; the ``repo:<name>`` convention works as elsewhere.
    """
    issue = payload.get("issue", {}) or {}
    fields = issue.get("fields", {}) or {}
    labels = [str(lab) for lab in fields.get("labels") or []]
    key = issue.get("key") or ""
    return RawTicket(
        issue_id=key,
        issue_key=key,
        title=fields.get("summary") or "",
        description=fields.get("description") or "",
        labels=labels,
        known_repositories=[
            lab[len(_REPO_LABEL_PREFIX):]
            for lab in labels
            if lab.startswith(_REPO_LABEL_PREFIX)
        ],
    )


def github_issue_payload_to_ticket(payload: dict[str, Any]) -> RawTicket:
    """Map a GitHub ``issues`` / ``issue_comment`` webhook payload to a ticket.

    The issue id is ``owner/repo#number`` and the issue key is the synthesised
    ``REPONAME-number`` (see ``connectors.github_issues``), so the rest of the
    pipeline - including PR correlation by key - works unchanged.
    """
    from foundry.connectors.github_issues import github_issue_key

    issue = payload.get("issue", {}) or {}
    repo = (payload.get("repository") or {}).get("full_name") or ""
    number = issue.get("number")

    labels = [
        lab["name"]
        for lab in (issue.get("labels") or [])
        if isinstance(lab, dict) and lab.get("name")
    ]
    # Explicit repo: labels win; otherwise the issue's host repo is the
    # (single) candidate. Never both - two confident candidates reads as
    # ambiguity and parks the run for a human.
    labelled_repos = [
        lab[len(_REPO_LABEL_PREFIX):]
        for lab in labels
        if lab.startswith(_REPO_LABEL_PREFIX)
    ]
    known_repositories = labelled_repos or ([repo] if repo else [])

    return RawTicket(
        issue_id=f"{repo}#{number}" if repo and number is not None else "",
        issue_key=github_issue_key(repo, number) if repo and number is not None else "",
        title=issue.get("title") or "",
        description=issue.get("body") or "",
        labels=labels,
        known_repositories=known_repositories,
    )
