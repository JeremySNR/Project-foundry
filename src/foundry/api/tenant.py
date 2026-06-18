"""Per-request tenant binding for the API surface (issue #156).

A pure-ASGI middleware that binds the active org (``db.tenant``) for the whole
duration of each HTTP request, *before* any route handler, dependency, or DB
session runs, and resets it afterwards. Being a raw ASGI middleware (not
``BaseHTTPMiddleware``) it runs in the request's own event-loop task, so the
contextvar it sets propagates into every downstream handler — including sync
endpoints dispatched to the threadpool, which copy the current context — and
covers endpoints that don't themselves authenticate (e.g. ``GET /runs``).

The org is derived **only** from a cryptographically-verified principal, never
from a request payload (invariant #5): the configured ``org_claim`` of an OIDC
bearer token (the API path) or, for the browser dashboard, the ``org`` that
``OidcLogin.complete`` stamped into the HMAC-signed SSO session cookie from the
verified id_token at login time (issue #34/#156). When multi-tenancy is not
configured (no ``org_claim`` — the default), or the request carries neither a
usable token nor a valid session cookie, the request runs in the single default
org, so a single-tenant deployment is byte-for-byte unchanged.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any, Callable

from foundry.api.oidc import OidcAuthError, OidcVerifier
from foundry.api.oidc_login import SESSION_COOKIE
from foundry.api.sessions import SessionSigner
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


def _session_cookie_from_scope(scope: dict[str, Any]) -> str | None:
    """The dashboard SSO session cookie value from an ASGI scope, or None."""
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar = SimpleCookie()
            jar.load(value.decode("latin-1"))
            morsel = jar.get(SESSION_COOKIE)
            return morsel.value if morsel is not None else None
    return None


def resolve_request_org(
    scope: dict[str, Any],
    *,
    verifier: OidcVerifier | None,
    org_claim: str | None,
    session_signer: SessionSigner | None = None,
) -> str:
    """The caller's org for this request, from the authenticated principal only.

    Two principals can carry an org, both cryptographically verified — never a
    request payload (invariant #5):

    * an **OIDC bearer token** (the API path): its verified ``org_claim``, or
    * the **dashboard SSO session cookie** (the browser path): the ``org`` value
      ``OidcLogin.complete`` stamped into the HMAC-signed cookie from the
      verified id_token at login time.

    The bearer token wins when both are present. Returns
    :data:`~foundry.db.tenant.DEFAULT_ORG_ID` unless multi-tenancy is configured
    (``org_claim`` set) and one of those principals yields a non-empty org, so a
    single-tenant deployment is byte-for-byte unchanged.
    """
    if not org_claim:
        return DEFAULT_ORG_ID
    if verifier is not None:
        token = _bearer_token_from_scope(scope)
        if token:
            try:
                claims = verifier.verify(token)
            except OidcAuthError:
                claims = None
            if claims is not None:
                value = claims.get(org_claim)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    if session_signer is not None:
        payload = session_signer.read(_session_cookie_from_scope(scope))
        if payload is not None:
            org = payload.get("org")
            if isinstance(org, str) and org.strip():
                return org.strip()
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
