"""OIDC bearer-token verification for the Foundry API (issue #34).

This is **additive** to the static ``FOUNDRY_API_TOKEN``: when an issuer,
audience, and JWKS URI are configured, a token-gated endpoint also accepts a
valid OIDC JWT in the ``Authorization: Bearer`` header. It is opt-in and
fail-closed - the default deployment (no OIDC config) is byte-for-byte
unchanged, and the static token keeps working alongside OIDC when both are set.

Design notes (so the next agent doesn't have to re-derive them):

* ``pyjwt[crypto]`` is the optional ``[oidc]`` extra, imported **lazily** inside
  the functions that need it. Importing this module never pulls in pyjwt, so the
  no-OIDC path - and the offline core suite - never require the extra. A
  deployment that configures OIDC without the extra installed fails loud at
  ``build_verifier`` (a deploy-time error, not a per-request surprise).
* The JWKS fetch goes through an injected ``fetch`` callable (a transport seam),
  so tests run fully offline against a locally-minted RSA key set and never hit
  the network (AGENTS.md invariant #3). The live fetcher uses httpx.
* Verification is hardened: an **algorithm allow-list** (RS256 by default) shuts
  off ``alg:none`` and HMAC-confusion (signing a token with the public key as an
  HMAC secret); ``iss``/``aud``/``exp`` are required and validated, with bounded
  clock-skew ``leeway``. A missing/unknown ``kid`` is refused.
* This slice covers **authentication** only (proving the caller may reach the
  API). Mapping IdP groups to approver roles, and binding the approval *actor*
  to a verified claim, are deliberate follow-ups - approver roles stay in
  committed config (invariant #5), never derived from a request here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# JWKS documents are small JSON mappings: ``{"keys": [ ... ]}``.
JwksFetcher = Callable[[], Mapping[str, Any]]

# Default signing-algorithm allow-list. Asymmetric RS256 only: symmetric HS*
# and the unsigned "none" algorithm are refused so a forged token can never be
# accepted by presenting the (public) JWKS key as an HMAC secret, or unsigned.
DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)

# How long a fetched JWKS is trusted before a refresh, and the floor between
# refreshes triggered by an unknown ``kid`` (so a flood of bogus-kid tokens
# can't hammer the IdP's JWKS endpoint).
_DEFAULT_JWKS_TTL_SECONDS = 600.0
_DEFAULT_MIN_REFRESH_SECONDS = 60.0


class OidcAuthError(Exception):
    """Raised when an OIDC bearer token fails verification (=> 401)."""


class OidcConfigError(Exception):
    """Raised when OIDC settings are partial/invalid or the extra is missing.

    Surfaced at app construction (a deploy-time, fail-closed error) rather than
    on the first request.
    """


@dataclass
class JwksCache:
    """Caches a JWKS, keyed by ``kid``, behind an injected ``fetch`` callable.

    Thread-safe: the FastAPI app serves requests concurrently, so the keyset and
    its fetch timestamp are guarded by a lock. The cache refreshes on TTL expiry
    and (rate-limited) when a token presents an unknown ``kid`` - the normal way
    a key rotation shows up.
    """

    fetch: JwksFetcher
    ttl_seconds: float = _DEFAULT_JWKS_TTL_SECONDS
    min_refresh_seconds: float = _DEFAULT_MIN_REFRESH_SECONDS
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _keyset: Any = field(default=None, repr=False)
    _fetched_at: float = field(default=0.0, repr=False)

    def signing_key(self, kid: str) -> Any:
        """Return the JWK whose ``kid`` matches, fetching/refreshing as needed.

        Raises ``OidcAuthError`` if no matching key can be found (after at most
        one rate-limited refresh) or the JWKS cannot be loaded.
        """
        with self._lock:
            now = self.clock()
            cache_fresh = (
                self._keyset is not None
                and (now - self._fetched_at) < self.ttl_seconds
            )
            if cache_fresh:
                key = self._find(kid)
                if key is not None:
                    return key
                # Unknown kid against a still-fresh set: likely a rotation.
                # Refetch once, but not more often than min_refresh_seconds.
                if (now - self._fetched_at) < self.min_refresh_seconds:
                    raise OidcAuthError(f"no signing key for kid {kid!r}")
            self._refresh(now)
            key = self._find(kid)
            if key is None:
                raise OidcAuthError(f"no signing key for kid {kid!r}")
            return key

    def _refresh(self, now: float) -> None:
        import jwt  # lazy: only when OIDC is actually exercised

        try:
            data = self.fetch()
            keyset = jwt.PyJWKSet.from_dict(dict(data))
        except Exception as exc:  # network error, malformed JWKS, ...
            raise OidcAuthError(f"could not load JWKS: {exc}") from exc
        self._keyset = keyset
        self._fetched_at = now

    def _find(self, kid: str) -> Any:
        if self._keyset is None:
            return None
        for key in self._keyset.keys:
            if key.key_id == kid:
                return key
        return None


@dataclass(frozen=True)
class OidcVerifier:
    """Verifies an OIDC JWT against a configured issuer/audience and JWKS."""

    issuer: str
    audience: str
    jwks: JwksCache
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    leeway_seconds: int = 60

    def verify(self, token: str) -> dict[str, Any]:
        """Return the validated claims, or raise ``OidcAuthError``."""
        import jwt  # lazy

        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise OidcAuthError("malformed token header") from exc

        alg = header.get("alg")
        if alg not in self.algorithms:
            # Defends against alg:none and HS/RS confusion.
            raise OidcAuthError(f"token algorithm {alg!r} not in allow-list")
        kid = header.get("kid")
        if not kid:
            raise OidcAuthError("token header missing 'kid'")

        signing_key = self.jwks.signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except OidcAuthError:
            raise
        except Exception as exc:  # jwt.InvalidTokenError and friends
            raise OidcAuthError(f"token rejected: {exc}") from exc
        return dict(claims)


def _live_jwks_fetcher(jwks_uri: str) -> JwksFetcher:
    """A network ``fetch`` for the JWKS, built on httpx (the live transport)."""

    def fetch() -> Mapping[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - live path only
            raise OidcAuthError(
                "httpx is required to fetch JWKS; install the 'http' extra"
            ) from exc
        response = httpx.get(jwks_uri, timeout=10.0)
        response.raise_for_status()
        return response.json()

    return fetch


def build_verifier(
    *,
    issuer: str,
    audience: str,
    jwks_uri: str,
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS,
    leeway_seconds: int = 60,
    fetch: JwksFetcher | None = None,
) -> OidcVerifier:
    """Construct an :class:`OidcVerifier`, failing loud if pyjwt is absent.

    ``fetch`` is the JWKS transport seam - tests inject a fake; production omits
    it and gets the live httpx fetcher for ``jwks_uri``.
    """
    try:
        import jwt  # noqa: F401  (presence check; used lazily at verify time)
    except ImportError as exc:
        raise OidcConfigError(
            "OIDC auth is configured but pyjwt is not installed; "
            "install the 'oidc' extra (pip install 'project-foundry[oidc]')"
        ) from exc
    if not algorithms:
        raise OidcConfigError("oidc algorithms allow-list must be non-empty")
    cache = JwksCache(fetch=fetch or _live_jwks_fetcher(jwks_uri))
    return OidcVerifier(
        issuer=issuer,
        audience=audience,
        jwks=cache,
        algorithms=tuple(algorithms),
        leeway_seconds=leeway_seconds,
    )
