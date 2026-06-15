"""Stateless, signed cookie payloads for the dashboard SSO flow (issue #34).

A tiny HMAC-SHA256 signer over a compact JSON payload - pure stdlib, no new
dependency (mirrors the "zero-build, zero-dep" posture of the dashboard page and
the in-process rate limiter). It backs two short-lived cookies in the browser
OIDC login flow:

* the **login-state** cookie that carries the CSRF ``state``, the OIDC ``nonce``
  and the PKCE ``code_verifier`` across the IdP round-trip (so the flow needs no
  server-side session store), and
* the **session** cookie minted after a successful login, carrying only the
  verified subject identity and an expiry.

The signer proves *integrity* (a tampered or forged payload is rejected) and
*freshness* (an embedded ``exp`` is enforced on read). It is **not** encryption:
the payload is signed, not hidden, so nothing secret goes in it - the session
cookie holds only the already-known subject identity. The secret is an env-only
credential (``FOUNDRY_SESSION_SECRET``), never committed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without padding (cookie-safe, no ``=`` / ``+`` / ``/``)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass(frozen=True)
class SessionSigner:
    """Signs/verifies short-lived JSON cookie payloads with HMAC-SHA256.

    ``clock`` is injectable so tests can drive expiry deterministically without
    sleeping (AGENTS.md invariant #3: no wall-clock dependence in tests).
    """

    secret: str
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("SessionSigner requires a non-empty secret")

    def _key(self) -> bytes:
        return self.secret.encode("utf-8")

    def mint(self, payload: dict[str, Any], *, ttl_seconds: int) -> str:
        """Return a signed token for ``payload`` that expires in ``ttl_seconds``.

        An ``exp`` field is added (absolute epoch seconds); any caller-supplied
        ``exp`` is overwritten so freshness is always enforced on read.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        body = dict(payload)
        body["exp"] = int(self.clock()) + int(ttl_seconds)
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded = _b64encode(raw)
        sig = hmac.new(self._key(), encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{_b64encode(sig)}"

    def read(self, token: str | None) -> dict[str, Any] | None:
        """Return the payload if the token is well-formed, unexpired and signed
        by this secret; otherwise ``None`` (never raises on bad input)."""
        if not token or "." not in token:
            return None
        encoded, _, provided_sig = token.partition(".")
        expected_sig = hmac.new(
            self._key(), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        try:
            given = _b64decode(provided_sig)
        except (ValueError, TypeError):
            return None
        # Constant-time signature comparison defeats timing oracles.
        if not hmac.compare_digest(expected_sig, given):
            return None
        try:
            payload = json.loads(_b64decode(encoded))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or self.clock() >= exp:
            return None
        return payload
