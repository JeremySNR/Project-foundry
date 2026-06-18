"""Map a verified webhook delivery to its tenant org (issue #34 follow-up).

Multi-tenancy (#156) isolates every read/write to the active org, which the API
binds per-request from a verified principal (`api/tenant.py`). But webhooks carry
no OIDC token — they authenticate with a per-provider shared secret — so every
webhook-created run and every webhook-driven PR observation ran in the **default**
org, no matter which tenant the delivery belonged to. This is the webhook twin of
the bearer-token / SSO-cookie org binding.

The verified principal of a webhook *is* the shared secret that authenticated it,
so the org is derived from **which committed secret matched**, never from the
(sender-controlled) payload (invariant #5). An operator gives each tenant org a
dedicated webhook secret (committed config, `FOUNDRY_WEBHOOK_ORG_SECRETS`); a
delivery validly signed (HMAC: Linear/GitHub) or tokened (GitLab/Jira) with org
X's secret binds tenant X for the lifetime of that delivery. The per-provider
*global* secrets continue to resolve to the default org, so a single-tenant
deployment (no per-org secrets configured) is byte-for-byte unchanged.

This is strictly a tenant-resolution read path: no gate rule, `PolicyInput` field,
or `foundry.rego` change (invariants #1/#2 don't apply). A per-org secret can only
ever route a delivery to *its own* org — it can never widen access to another
tenant's rows, because the org it resolves to is the very credential that proved
the sender's identity.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from foundry.api.security import verify_signature
from foundry.db.tenant import DEFAULT_ORG_ID


@dataclass(frozen=True)
class WebhookOrgSecrets:
    """A committed map of per-org webhook secrets, with constant-time lookup.

    Empty (the default) means single-tenant: no delivery ever resolves to a
    non-default org and behaviour is unchanged.
    """

    by_org: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_pairs(cls, pairs) -> "WebhookOrgSecrets":
        """Build (and fail-closed validate) from ``(org_id, secret)`` pairs.

        Rejects, at config-load time: a blank org id, the reserved
        :data:`~foundry.db.tenant.DEFAULT_ORG_ID` (the global secrets already own
        the default org), a blank secret, a duplicate org, or a secret reused
        across orgs (which would make a delivery's org ambiguous).
        """
        cleaned: list[tuple[str, str]] = []
        seen_orgs: set[str] = set()
        seen_secrets: set[str] = set()
        for org, secret in pairs:
            org = (org or "").strip()
            secret = (secret or "").strip()
            if not org:
                raise ValueError("webhook_org_secrets: org id must be non-empty")
            if org == DEFAULT_ORG_ID:
                raise ValueError(
                    "webhook_org_secrets: org id must not be the reserved default "
                    f"org {DEFAULT_ORG_ID!r} (the global webhook secrets map to it)"
                )
            if not secret:
                raise ValueError(
                    f"webhook_org_secrets: secret for org {org!r} must be non-empty"
                )
            if org in seen_orgs:
                raise ValueError(f"webhook_org_secrets: duplicate org {org!r}")
            if secret in seen_secrets:
                raise ValueError(
                    "webhook_org_secrets: a secret is reused across orgs; each org "
                    "needs a distinct secret so a delivery maps to one org only"
                )
            seen_orgs.add(org)
            seen_secrets.add(secret)
            cleaned.append((org, secret))
        return cls(tuple(cleaned))

    def is_empty(self) -> bool:
        return not self.by_org

    def secrets(self) -> tuple[str, ...]:
        """The configured per-org secrets (for collision checks at app build)."""
        return tuple(secret for _, secret in self.by_org)

    def resolve_hmac(self, body: bytes, signature: str | None) -> str | None:
        """The org whose secret HMAC-verifies ``body``/``signature``, or None.

        Every candidate is checked (constant-time per candidate) so a match never
        short-circuits on a timing side channel against the org list.
        """
        matched: str | None = None
        for org, secret in self.by_org:
            if verify_signature(secret, body, signature):
                matched = org
        return matched

    def resolve_token(self, token: str | None) -> str | None:
        """The org whose secret equals ``token`` (constant-time), or None."""
        supplied = token or ""
        if not supplied:
            return None
        matched: str | None = None
        for org, secret in self.by_org:
            if hmac.compare_digest(secret, supplied):
                matched = org
        return matched


def org_for_hmac(
    *,
    default_secret: str | None,
    tenants: WebhookOrgSecrets,
    body: bytes,
    signature: str | None,
) -> str | None:
    """Resolve the org of an HMAC-signed delivery (Linear / GitHub).

    The per-provider *global* secret resolves to the default org (the historical
    single-tenant path, checked first); otherwise a per-org secret's org. Returns
    ``None`` when the signature verifies against no configured secret — the caller
    rejects that as a 401, exactly as before.
    """
    if default_secret and verify_signature(default_secret, body, signature):
        return DEFAULT_ORG_ID
    return tenants.resolve_hmac(body, signature)


def org_for_token(
    *,
    default_secret: str | None,
    tenants: WebhookOrgSecrets,
    token: str | None,
) -> str | None:
    """Resolve the org of a shared-token delivery (GitLab / Jira).

    The global secret resolves to the default org (checked first); otherwise a
    per-org secret's org. ``None`` => the token matched no configured secret.
    """
    if default_secret and token and hmac.compare_digest(default_secret, token):
        return DEFAULT_ORG_ID
    return tenants.resolve_token(token)
