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
