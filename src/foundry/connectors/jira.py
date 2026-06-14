"""Jira as the issue tracker.

Implements the ``IssueTracker`` protocol over the Jira Cloud REST API (v2
endpoints, which accept and return plain-text bodies). Jira issue keys
(``ACME-42``) already match the pattern PR correlation scans for, so the
delegated-agent loop closes with no key synthesis at all.

State mapping: Jira workflows are customer-specific, so ``set_state`` looks up
the issue's *available transitions* and fires the one whose target status best
matches Foundry's state name. When nothing matches it does nothing - Foundry
never invents workflow states in someone's Jira project.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from foundry.schemas.ticket import RawTicket

# transport(method, path, body=None) -> parsed JSON (jira_transport shape)
Transport = Callable[..., Any]

_log = logging.getLogger(__name__)


class JiraConnector:
    """IssueTracker over the Jira Cloud REST API."""

    def __init__(self, *, transport: Transport) -> None:
        self._transport = transport

    def get_issue(self, issue_id: str) -> RawTicket:
        data = self._transport(
            "GET", f"/rest/api/2/issue/{issue_id}?fields=summary,description,labels"
        )
        fields = data.get("fields") or {}
        labels = [str(lab) for lab in fields.get("labels") or []]
        return RawTicket(
            issue_id=data.get("key") or issue_id,
            issue_key=data.get("key") or issue_id,
            title=fields.get("summary") or "",
            description=fields.get("description") or "",
            labels=labels,
            known_repositories=[
                lab[len("repo:"):] for lab in labels if lab.startswith("repo:")
            ],
        )

    def post_comment(self, issue_id: str, body: str) -> None:
        self._transport("POST", f"/rest/api/2/issue/{issue_id}/comment", {"body": body})

    def set_state(self, issue_id: str, state_name: str) -> None:
        """Fire the available transition that best matches the target state."""
        data = self._transport("GET", f"/rest/api/2/issue/{issue_id}/transitions")
        wanted = _normalise(state_name)
        for transition in data.get("transitions") or []:
            target = _normalise(
                (transition.get("to") or {}).get("name") or transition.get("name") or ""
            )
            if target and _matches(wanted, target):
                self._transport(
                    "POST",
                    f"/rest/api/2/issue/{issue_id}/transitions",
                    {"transition": {"id": transition["id"]}},
                )
                return
        _log.info(
            "no Jira transition on %s matches %r; leaving workflow state alone",
            issue_id,
            state_name,
        )


def _normalise(name: str) -> str:
    return name.lower().replace("foundry:", "").strip()


def _matches(wanted: str, target: str) -> bool:
    """Whole-phrase match between a Foundry state and a Jira transition target.

    Plain substring matching was bidirectional and unsafe: target ``"unblocked"``
    contains ``"blocked"``, so asking to move to ``Blocked`` could fire a
    transition to *Unblocked*. Require either an exact match or that one phrase
    appears in the other on word boundaries (so ``"in progress"`` still matches
    ``"in progress (dev)"`` but ``"blocked"`` never matches ``"unblocked"``).
    """
    if wanted == target:
        return True
    return _phrase_in(wanted, target) or _phrase_in(target, wanted)


def _phrase_in(needle: str, haystack: str) -> bool:
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None
