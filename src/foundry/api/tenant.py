"""Per-request tenant binding for the API surface (issue #156).

A pure-ASGI middleware that binds the active org (``db.tenant``) for the whole
duration of each HTTP request, *before* any route handler, dependency, or DB
session runs, and resets it afterwards. Being a raw ASGI middleware (not
``BaseHTTPMiddleware``) it runs in the request's own event-loop task, so the
contextvar it sets propagates into every downstream handler — including sync
endpoints dispatched to the threadpool, which copy the current context — and
covers endpoints that don't themselves authenticate (e.g. ``GET /runs``).

The org is derived **only** from the cryptographically-verified OIDC bearer
token (its configured ``org_claim``), never from a request payload (invariant
#5). When multi-tenancy is not configured (no ``org_claim`` or no verifier — the
default), or the request carries no usable token, the request runs in the single
default org, so a single-tenant deployment is byte-for-byte unchanged and pays
no extra token verification.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.api.oidc import OidcAuthError, OidcVerifier
from foundry.db.tenant import DEFAULT_ORG_ID, reset_current_org, set_current_org


def _bearer_token_from_scope(scope: dict[str, Any]) -> str | None:
    """The bearer token from an ASGI scope's Authorization header, or None."""
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            text = value.decode("latin-1")
            if text.startswith("Bearer "):
                token = text[len("Bearer ") :].strip()
                return token or None
            return None
    return None


def resolve_request_org(
    scope: dict[str, Any],
    *,
    verifier: OidcVerifier | None,
    org_claim: str | None,
) -> str:
    """The caller's org for this request, from the verified token only.

    Returns :data:`~foundry.db.tenant.DEFAULT_ORG_ID` unless multi-tenancy is
    configured (``org_claim`` set, a verifier present) *and* the request carries
    a valid OIDC bearer token whose ``org_claim`` is a non-empty string.
    """
    if not org_claim or verifier is None:
        return DEFAULT_ORG_ID
    token = _bearer_token_from_scope(scope)
    if not token:
        return DEFAULT_ORG_ID
    try:
        claims = verifier.verify(token)
    except OidcAuthError:
        return DEFAULT_ORG_ID
    value = claims.get(org_claim)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_ORG_ID


class TenantMiddleware:
    """Bind the active org for the lifetime of each HTTP request."""

    def __init__(self, app, *, resolve_org: Callable[[dict[str, Any]], str]) -> None:
        self.app = app
        self._resolve_org = resolve_org

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = set_current_org(self._resolve_org(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_org(token)
