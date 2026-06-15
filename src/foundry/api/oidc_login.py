"""Browser-side OIDC login (SSO) for the Foundry dashboard (issue #34).

The bearer-token slice (#86) let the *API* accept an OIDC JWT; this adds the
**interactive browser login** the dashboard needs so an operator signs in with
their IdP instead of pasting a token. It is the standard OAuth 2.0
**authorization-code flow with PKCE**:

1. ``GET /dashboard/login`` mints CSRF ``state``, an OIDC ``nonce`` and a PKCE
   ``code_verifier`` (stashed in a short-lived, signed login-state cookie - no
   server-side session store), then 302-redirects to the IdP's authorize URL.
2. The IdP authenticates the user and redirects back to
   ``GET /dashboard/auth/callback`` with an authorization ``code``.
3. The callback verifies ``state``, exchanges the ``code`` at the IdP token
   endpoint (over an injected transport seam) for an ``id_token``, verifies that
   token (reusing the hardened :class:`~foundry.api.oidc.OidcVerifier`, audience
   = the client id), checks the ``nonce``, and mints a signed **session cookie**
   carrying the verified subject identity.

The session cookie then authenticates the dashboard's read calls (the
``_require_api_token`` read path accepts it). It is **read-only**: it is
deliberately *not* accepted on the approval endpoint, so a cookie-bearing
browser cannot be tricked (CSRF) into driving an approval - approvals still
require a bearer token or a signed webhook.

Design (mirrors the bearer slice so the next agent doesn't re-derive it):

* Additive, opt-in, fail-closed. Wired only when the browser-login config is
  complete (client id, authorize/token endpoints, redirect URI) **and** the
  env-only secrets (client secret, session secret) are set; otherwise the login
  routes 403 and the default deployment is byte-for-byte unchanged.
* The token-endpoint call goes through an injected ``exchange`` seam, so tests
  run fully offline with a locally-minted id_token and never hit the network
  (invariant #3). The live exchanger uses httpx (the ``[http]`` extra).
* PKCE (S256) + ``state`` + ``nonce`` are all enforced: ``state`` defeats login
  CSRF, ``nonce`` binds the id_token to this request (anti-replay), PKCE binds
  the code to this client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .oidc import OidcVerifier, identity_from_claims
from .sessions import SessionSigner

# The token-endpoint transport seam: given the POST form parameters, return the
# decoded token response (a JSON mapping carrying ``id_token``). Tests inject a
# fake; production omits it and gets the live httpx exchanger.
TokenExchanger = Callable[[Mapping[str, str]], Mapping[str, Any]]

# Cookie names. The login-state cookie is scoped to the login routes; the
# session cookie is site-wide so it reaches the token-gated read endpoints.
LOGIN_STATE_COOKIE = "foundry_login"
SESSION_COOKIE = "foundry_session"

_DEFAULT_SCOPES: tuple[str, ...] = ("openid", "email")
_DEFAULT_SESSION_TTL = 8 * 60 * 60  # 8 hours
_DEFAULT_LOGIN_TTL = 10 * 60  # 10 minutes to complete the IdP round-trip


class OidcLoginError(Exception):
    """A recoverable failure in the browser login flow (=> HTTP 400)."""


def pkce_challenge(verifier: str) -> str:
    """The S256 PKCE code challenge for ``verifier`` (base64url, no padding)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _live_token_exchanger(token_endpoint: str) -> TokenExchanger:
    """A network ``exchange`` for the token endpoint, built on httpx (live)."""

    def exchange(form: Mapping[str, str]) -> Mapping[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - live path only
            raise OidcLoginError(
                "httpx is required for the OIDC token exchange; install the "
                "'http' extra"
            ) from exc
        response = httpx.post(token_endpoint, data=dict(form), timeout=10.0)
        response.raise_for_status()
        return response.json()

    return exchange


@dataclass(frozen=True)
class OidcLogin:
    """Drives the authorization-code-with-PKCE browser login.

    The HTTP layer (``api/app.py``) is a thin shell over :meth:`begin` and
    :meth:`complete`; the flow logic lives here so it unit-tests without a
    request/response cycle.
    """

    client_id: str
    client_secret: str
    authorization_endpoint: str
    token_endpoint: str
    redirect_uri: str
    verifier: OidcVerifier
    signer: SessionSigner
    subject_claim: str = "email"
    scopes: tuple[str, ...] = _DEFAULT_SCOPES
    session_ttl_seconds: int = _DEFAULT_SESSION_TTL
    login_ttl_seconds: int = _DEFAULT_LOGIN_TTL
    cookie_secure: bool = True
    # RP-Initiated Logout 1.0: the IdP's end-session endpoint and (optionally)
    # where it returns the browser afterwards. Both None => no federated logout
    # (the logout route just clears the local cookie). See ``end_session_url``.
    end_session_endpoint: str | None = None
    post_logout_redirect_uri: str | None = None
    exchange: TokenExchanger = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.exchange is None:
            object.__setattr__(
                self, "exchange", _live_token_exchanger(self.token_endpoint)
            )

    def begin(self) -> tuple[str, str]:
        """Start a login: return ``(authorize_url, login_state_cookie_value)``.

        The caller 302-redirects the browser to ``authorize_url`` and sets the
        signed login-state cookie so the callback can recover ``state``/``nonce``
        /``code_verifier`` (no server-side store).
        """
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in self.authorization_endpoint else "?"
        authorize_url = self.authorization_endpoint + sep + urllib.parse.urlencode(params)
        cookie = self.signer.mint(
            {"s": state, "n": nonce, "v": code_verifier},
            ttl_seconds=self.login_ttl_seconds,
        )
        return authorize_url, cookie

    def complete(
        self, *, code: str | None, state: str | None, login_cookie: str | None
    ) -> str:
        """Finish a login: validate the callback and return the session cookie.

        Raises :class:`OidcLoginError` on any recoverable failure (expired login,
        ``state`` mismatch, bad token response, ``nonce`` mismatch, no subject) -
        the caller maps it to a 400. A token that fails cryptographic
        verification raises :class:`~foundry.api.oidc.OidcAuthError`.
        """
        stash = self.signer.read(login_cookie)
        if stash is None:
            raise OidcLoginError("login session expired or missing; retry the login")
        if not code:
            raise OidcLoginError("authorization response missing 'code'")
        expected_state = str(stash.get("s", ""))
        if not expected_state or not hmac.compare_digest(expected_state, str(state or "")):
            raise OidcLoginError("state mismatch; possible CSRF, login aborted")

        token_response = self.exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code_verifier": str(stash.get("v", "")),
            }
        )
        id_token = (token_response or {}).get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise OidcLoginError("token response did not include an id_token")

        # Verifies signature/iss/aud/exp via the shared hardened verifier
        # (audience = client id for an id_token). Raises OidcAuthError if bad.
        claims = self.verifier.verify(id_token)

        # Bind the id_token to *this* login request (anti-replay/injection).
        expected_nonce = str(stash.get("n", ""))
        if not expected_nonce or not hmac.compare_digest(
            expected_nonce, str(claims.get("nonce", ""))
        ):
            raise OidcLoginError("id_token nonce mismatch")

        identity = identity_from_claims(claims, self.subject_claim)
        if identity is None:
            raise OidcLoginError("id_token has no usable subject claim")
        return self.signer.mint({"sub": identity}, ttl_seconds=self.session_ttl_seconds)

    def end_session_url(self) -> str | None:
        """The IdP RP-initiated logout URL, or ``None`` if not configured.

        Per OpenID Connect RP-Initiated Logout 1.0, ``client_id`` is sent in lieu
        of an ``id_token_hint`` so the OP can identify this RP (and so we don't
        have to stash the id_token in the signed session cookie - keeping the
        cookie's contents, and the login path, byte-for-byte unchanged). The
        optional ``post_logout_redirect_uri`` (which the OP must have registered)
        is where the browser is returned after the IdP session is terminated.

        The URL is built entirely from committed config - no request/user input
        reaches it - so it is not an open-redirect surface.
        """
        if not self.end_session_endpoint:
            return None
        params = {"client_id": self.client_id}
        if self.post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = self.post_logout_redirect_uri
        sep = "&" if "?" in self.end_session_endpoint else "?"
        return self.end_session_endpoint + sep + urllib.parse.urlencode(params)


__all__ = [
    "LOGIN_STATE_COOKIE",
    "SESSION_COOKIE",
    "OidcLogin",
    "OidcLoginError",
    "TokenExchanger",
    "pkce_challenge",
]
