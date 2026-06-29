"""Microsoft Teams Outgoing-Webhook activities -> Foundry approval decisions.

The Teams approval surface is the mirror image of the Slack one (``api/slack.py``)
with Teams-native primitives: Teams signs the request (an HMAC over the raw body,
verified in ``security.py``), and the *message the approver typed* (after
@mentioning the bot) carries the decision verb and which run it targets. This
module turns a Bot Framework ``message`` activity into a normalised decision so
the endpoint in ``app.py`` can drive the same ``_apply_decision`` path as every
other surface - same policy gate, same config-derived role checks, same audit
writes.

Conventions Foundry expects on the message the approver sends the bot:

* the text, once the ``@Foundry`` mention is stripped, is
  ``<verb> <issue-id>`` (an optional ``/foundry`` prefix is tolerated, matching
  the tracker comment command) where ``<verb>`` is ``approve`` / ``reject`` /
  ``stop`` - the same verbs the Slack buttons expose.
* the acting user is ``from.aadObjectId`` (the Entra/AAD object id) falling back
  to ``from.id`` - the workspace identity Teams signs into the payload, so it
  cannot be forged by anyone who is not Teams. Configure approvers by that id,
  exactly as Slack approvers are keyed by ``user.id``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

# The verb vocabulary (and the ``/foundry`` prefix tolerance) is owned by the
# outbound renderer that prints the command syntax onto the approval card;
# re-exported here so the parser and existing importers keep a single source of
# truth. Other /foundry verbs are not exposed on the Teams card.
from foundry.connectors.teams import TEAMS_COMMAND_PREFIX, TEAMS_DECISIONS

__all__ = [
    "TEAMS_COMMAND_PREFIX",
    "TEAMS_DECISIONS",
    "TeamsInteraction",
    "format_teams_reply",
    "parse_teams_interaction",
]

# Teams wraps a bot @mention in an ``<at>display name</at>`` span. Drop the whole
# span (so the bot's display name isn't parsed as a command token), then strip
# any other inline HTML, before parsing the command tokens.
_MENTION = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class TeamsInteraction:
    command: str
    issue_id: str
    user: str


def _strip_mentions(text: str) -> str:
    """Remove the ``<at>Foundry</at>`` mention span and any inline HTML/entities.

    Tags are dropped first, then HTML entities are decoded (so a ``&nbsp;`` Teams
    sometimes inserts after the mention becomes a real space the tokeniser splits
    on, and an escaped ``&lt;`` can't reintroduce a tag).
    """
    return html.unescape(_TAG.sub(" ", _MENTION.sub(" ", text)))


def parse_teams_interaction(payload: dict) -> TeamsInteraction | None:
    """Extract the approval decision from a Teams ``message`` activity.

    Returns ``None`` when the payload is not an actionable Foundry decision (not
    a message activity, no recognised verb, or a missing issue id / user).
    Returning ``None`` rather than raising lets the endpoint acknowledge
    unrelated activities with a 200 instead of making Teams retry-deliver them.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "message":
        return None

    sender = payload.get("from") or {}
    user = sender.get("aadObjectId") or sender.get("id") or ""
    if not user:
        return None

    text = _strip_mentions(str(payload.get("text") or "")).strip()
    if not text:
        return None
    tokens = text.split()
    # Tolerate a leading ``/foundry`` (the tracker-comment command form).
    if tokens and tokens[0].lower() == TEAMS_COMMAND_PREFIX:
        tokens = tokens[1:]
    if len(tokens) < 2:
        return None
    command = tokens[0].lower()
    if command not in TEAMS_DECISIONS:
        return None
    issue_id = " ".join(tokens[1:]).strip()
    if not issue_id:
        return None
    return TeamsInteraction(command=command, issue_id=issue_id, user=str(user))


def format_teams_reply(result: dict[str, Any]) -> dict[str, Any]:
    """Render an ``_apply_decision`` result as a Teams message activity.

    The HTTP response to an Outgoing Webhook is shown back to the approver in the
    channel, so turn the decision outcome into a short human line rather than
    echoing the raw status dict.
    """
    status = result.get("status")
    if status == "applied":
        command = result.get("command", "decision")
        run = result.get("run") or {}
        run_status = run.get("status", "updated")
        text = f"Foundry: {command} applied — run is now `{run_status}`."
    elif status == "refused":
        text = f"Foundry: decision refused — {result.get('reason', 'not allowed')}."
    else:
        text = f"Foundry: no action — {result.get('reason', 'not an actionable decision')}."
    return {"type": "message", "text": text}
