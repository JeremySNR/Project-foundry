"""Slack interactivity payloads -> Foundry approval decisions.

The inbound Slack approval surface is the mirror image of the Linear/GitHub/Jira
comment surfaces: Slack signs the request (verified in ``security.py``), and the
*action the user clicked* carries the decision and which run it targets. This
module turns a Slack ``block_actions`` payload into a normalised decision so the
endpoint in ``app.py`` can drive the same ``_apply_decision`` path as every other
surface - same policy gate, same config-derived role checks, same audit writes.

Conventions Foundry expects on the interactive message it (later) posts:

* ``action_id`` is ``foundry_approve`` / ``foundry_reject`` / ``foundry_stop``
  - the verb after the ``foundry_`` prefix is the approval command.
* the button ``value`` is the issue id the decision applies to.
* the acting user is ``user.id`` - the Slack workspace identity Slack signs into
  the payload, so it cannot be forged by anyone who is not Slack. Configure
  approvers by Slack user id, exactly as GitHub Issues approvers are keyed by
  login rather than email.
"""

from __future__ import annotations

from dataclasses import dataclass

# Decisions the approval surfaces accept. Mirrors the set the comment surfaces
# act on in app.py (approve/reject/stop); other /foundry verbs are not exposed
# as Slack buttons.
SLACK_ACTION_PREFIX = "foundry_"
SLACK_DECISIONS = frozenset({"approve", "reject", "stop"})


@dataclass(frozen=True)
class SlackInteraction:
    command: str
    issue_id: str
    user: str


def parse_slack_interaction(payload: dict) -> SlackInteraction | None:
    """Extract the approval decision from a Slack ``block_actions`` payload.

    Returns ``None`` when the payload is not an actionable Foundry decision (wrong
    interaction type, no recognised ``foundry_*`` action, or a missing issue id /
    user). Returning ``None`` rather than raising lets the endpoint acknowledge
    unrelated interactions with a 200 instead of making Slack retry-deliver them.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "block_actions":
        return None

    user = (payload.get("user") or {}).get("id") or ""
    if not user:
        return None

    for action in payload.get("actions") or []:
        if not isinstance(action, dict):
            continue
        action_id = action.get("action_id") or ""
        if not action_id.startswith(SLACK_ACTION_PREFIX):
            continue
        command = action_id[len(SLACK_ACTION_PREFIX) :]
        if command not in SLACK_DECISIONS:
            continue
        issue_id = (action.get("value") or "").strip()
        if not issue_id:
            continue
        return SlackInteraction(command=command, issue_id=issue_id, user=str(user))

    return None
