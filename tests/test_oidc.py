"""OIDC bearer-token auth (issue #34): verifier hardening + API integration.

Fully offline (AGENTS.md invariant #3): we mint a throwaway RSA key locally and
serve its public half through a fake JWKS ``fetch`` callable - no network, no
external IdP. ``importorskip`` keeps the suite green where the optional ``[oidc]``
extra (pyjwt[crypto]) is not installed.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from foundry.agents.manual import InMemoryFakeProvider  # noqa: E402
from foundry.api import create_app  # noqa: E402
from foundry.api.oidc import (  # noqa: E402
    JwksCache,
    OidcAuthError,
    build_verifier,
)
from foundry.config import Settings  # noqa: E402
from foundry.db import create_all, make_engine, make_session_factory  # noqa: E402
from foundry.orchestrator import FoundryOrchestrator  # noqa: E402

ISSUER = "https://idp.example.com/"
AUDIENCE = "foundry-api"
KID = "key-1"


def _keypair(kid: str = KID):
    """An RSA private key plus a one-key JWKS exposing its public half."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv, {"keys": [jwk]}


def _token(
    priv,
    *,
    kid: str = KID,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    alg: str = "RS256",
    exp_delta: int = 3600,
    nbf_delta: int | None = None,
    key=None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": "alice@example.com",
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_delta,
    }
    if nbf_delta is not None:
        claims["nbf"] = now + nbf_delta
    return jwt.encode(claims, key or priv, algorithm=alg, headers={"kid": kid})


def _verifier(jwks, *, algorithms=("RS256",), leeway_seconds=60):
    return build_verifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="https://idp.example.com/jwks",
        algorithms=algorithms,
        leeway_seconds=leeway_seconds,
        fetch=lambda: jwks,
    )


# --------------------------------------------------------------------------- #
# Verifier unit tests
# --------------------------------------------------------------------------- #


def test_valid_token_returns_claims():
    priv, jwks = _keypair()
    claims = _verifier(jwks).verify(_token(priv))
    assert claims["sub"] == "alice@example.com"
    assert claims["iss"] == ISSUER


def test_expired_token_rejected():
    priv, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks, leeway_seconds=0).verify(_token(priv, exp_delta=-10))


def test_not_yet_valid_token_rejected():
    priv, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks, leeway_seconds=0).verify(_token(priv, nbf_delta=300))


def test_wrong_issuer_rejected():
    priv, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify(_token(priv, issuer="https://evil.example.com/"))


def test_wrong_audience_rejected():
    priv, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify(_token(priv, audience="some-other-api"))


def test_algorithm_not_in_allow_list_rejected():
    # An RS256 token presented where only RS512 is allowed: refused at the
    # header allow-list, before any signature work (alg:none / confusion guard).
    priv, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks, algorithms=("RS512",)).verify(_token(priv))


def test_hs256_confusion_token_rejected():
    # Classic confusion attack: present a token with an HS256 header (an
    # attacker would sign it with the verifier's public key material as the HMAC
    # secret). The RS256 allow-list refuses the HS256 header outright, before any
    # secret is even considered - so a plain secret here is enough to exercise it
    # (and newer pyjwt refuses to *encode* with a JWK-shaped secret anyway).
    _, jwks = _keypair()
    forged = jwt.encode(
        {"sub": "mallory", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60},
        "public-key-bytes-as-hmac-secret",
        algorithm="HS256",
        headers={"kid": KID},
    )
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify(forged)


def test_missing_kid_rejected():
    priv, jwks = _keypair()
    token = jwt.encode(
        {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60},
        priv,
        algorithm="RS256",
    )  # no kid header
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify(token)


def test_unknown_kid_rejected():
    priv, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify(_token(priv, kid="some-other-kid"))


def test_bad_signature_rejected():
    # Token signed by a different key than the one published under this kid.
    priv, jwks = _keypair()
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _token(priv, key=attacker_key)
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify(forged)


def test_malformed_token_rejected():
    _, jwks = _keypair()
    with pytest.raises(OidcAuthError):
        _verifier(jwks).verify("not-a-jwt")


def test_jwks_load_failure_rejected():
    def boom():
        raise RuntimeError("network down")

    verifier = build_verifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="https://idp.example.com/jwks",
        fetch=boom,
    )
    priv, _ = _keypair()
    with pytest.raises(OidcAuthError):
        verifier.verify(_token(priv))


# --------------------------------------------------------------------------- #
# JWKS cache behaviour (rotation + refresh rate-limiting)
# --------------------------------------------------------------------------- #


def test_jwks_cache_refetches_on_rotation():
    _, set_a = _keypair("key-1")
    _, set_b = _keypair("key-2")
    sets = [set_a, {"keys": set_a["keys"] + set_b["keys"]}]
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def fetch():
        idx = min(calls["n"], len(sets) - 1)
        calls["n"] += 1
        return sets[idx]

    cache = JwksCache(
        fetch=fetch,
        min_refresh_seconds=10.0,
        clock=lambda: clock["t"],
    )
    assert cache.signing_key("key-1").key_id == "key-1"  # first fetch -> set A
    # Rotation: key-2 unknown to the cached set. Within the refresh floor it
    # must NOT refetch (rate-limited) and the lookup fails.
    with pytest.raises(OidcAuthError):
        cache.signing_key("key-2")
    assert calls["n"] == 1
    # Past the floor, a single refetch picks up the rotated key.
    clock["t"] += 20.0
    assert cache.signing_key("key-2").key_id == "key-2"
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Config (fail-closed all-or-nothing)
# --------------------------------------------------------------------------- #


def test_settings_partial_oidc_config_rejected():
    with pytest.raises(ValueError):
        Settings.load(env={"FOUNDRY_OIDC_ISSUER": ISSUER})


def test_settings_full_oidc_config_enabled():
    s = Settings.load(
        env={
            "FOUNDRY_OIDC_ISSUER": ISSUER,
            "FOUNDRY_OIDC_AUDIENCE": AUDIENCE,
            "FOUNDRY_OIDC_JWKS_URI": "https://idp.example.com/jwks",
            "FOUNDRY_OIDC_ALGORITHMS": "RS256, RS512",
            "FOUNDRY_OIDC_LEEWAY_SECONDS": "30",
        }
    )
    assert s.oidc_enabled is True
    assert s.oidc_algorithms == ("RS256", "RS512")
    assert s.oidc_leeway_seconds == 30


def test_settings_default_no_oidc():
    assert Settings.load().oidc_enabled is False


def test_settings_oidc_from_yaml(tmp_path):
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "auth:\n"
        "  oidc:\n"
        f"    issuer: {ISSUER}\n"
        f"    audience: {AUDIENCE}\n"
        "    jwks_uri: https://idp.example.com/jwks\n"
        "    algorithms: [RS256, RS384]\n"
        "    leeway_seconds: 15\n"
    )
    s = Settings.load(cfg)
    assert s.oidc_enabled is True
    assert s.oidc_algorithms == ("RS256", "RS384")
    assert s.oidc_leeway_seconds == 15


# --------------------------------------------------------------------------- #
# API integration
# --------------------------------------------------------------------------- #

ENDPOINT = "/metrics/delivery"


def _client(*, api_token=None, jwks=None):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    verifier = _verifier(jwks) if jwks is not None else None
    return TestClient(
        create_app(
            webhook_secret="s",
            session_factory=sf,
            orchestrator=orch,
            api_token=api_token,
            oidc_verifier=verifier,
        )
    )


def test_api_accepts_valid_oidc_token():
    priv, jwks = _keypair()
    client = _client(jwks=jwks)
    resp = client.get(
        ENDPOINT, headers={"Authorization": f"Bearer {_token(priv)}"}
    )
    assert resp.status_code == 200


def test_api_rejects_invalid_oidc_token():
    priv, jwks = _keypair()
    client = _client(jwks=jwks)
    bad = _token(priv, issuer="https://evil.example.com/")
    resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 401


def test_api_rejects_missing_token_when_oidc_only():
    _, jwks = _keypair()
    client = _client(jwks=jwks)
    assert client.get(ENDPOINT).status_code == 401


def test_static_token_still_works_alongside_oidc():
    priv, jwks = _keypair()
    client = _client(api_token="static-tok", jwks=jwks)
    # static token path
    assert (
        client.get(ENDPOINT, headers={"Authorization": "Bearer static-tok"}).status_code
        == 200
    )
    # oidc path on the same app
    assert (
        client.get(
            ENDPOINT, headers={"Authorization": f"Bearer {_token(priv)}"}
        ).status_code
        == 200
    )
    # neither credential
    assert (
        client.get(ENDPOINT, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


def test_no_auth_configured_disables_endpoint():
    client = _client(api_token=None, jwks=None)
    assert client.get(ENDPOINT).status_code == 403


def test_dashboard_served_when_only_oidc_configured():
    _, jwks = _keypair()
    client = _client(jwks=jwks)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
