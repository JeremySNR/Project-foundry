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
    sub: str = "alice@example.com",
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_delta,
    }
    if extra:
        claims.update(extra)
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


# --------------------------------------------------------------------------- #
# Claim-extraction helpers (pure, issue #34)
# --------------------------------------------------------------------------- #

from foundry.api.oidc import (  # noqa: E402
    groups_from_claims,
    identity_from_claims,
)


def test_identity_prefers_subject_claim_then_sub():
    # subject_claim present -> used.
    assert identity_from_claims({"email": "a@x", "sub": "u-1"}, "email") == "a@x"
    # subject_claim absent -> falls back to the standard 'sub'.
    assert identity_from_claims({"sub": "u-1"}, "email") == "u-1"
    # neither present (or blank) -> None, so the caller refuses to bind.
    assert identity_from_claims({"email": "   "}, "email") is None
    assert identity_from_claims({}, "email") is None


def test_groups_tolerates_list_and_space_delimited_string():
    assert groups_from_claims({"groups": ["a", "b"]}, "groups") == frozenset({"a", "b"})
    assert groups_from_claims({"groups": "a b c"}, "groups") == frozenset({"a", "b", "c"})
    # Missing / wrong-typed claim -> fail-closed empty set, never an error.
    assert groups_from_claims({}, "groups") == frozenset()
    assert groups_from_claims({"groups": 42}, "groups") == frozenset()


# --------------------------------------------------------------------------- #
# Config (IdP-group -> role map, issue #34)
# --------------------------------------------------------------------------- #


def test_group_role_map_rejects_unknown_role_at_load(tmp_path):
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "auth:\n"
        "  oidc:\n"
        f"    issuer: {ISSUER}\n"
        f"    audience: {AUDIENCE}\n"
        "    jwks_uri: https://idp.example.com/jwks\n"
        "    group_role_map:\n"
        "      eng-leads: [engineering, wizard]\n"  # 'wizard' is not a role
    )
    with pytest.raises(ValueError, match="unknown approval roles"):
        Settings.load(cfg)


def test_group_role_map_and_claims_from_config(tmp_path):
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "auth:\n"
        "  oidc:\n"
        f"    issuer: {ISSUER}\n"
        f"    audience: {AUDIENCE}\n"
        "    jwks_uri: https://idp.example.com/jwks\n"
        "    subject_claim: email\n"
        "    group_claim: roles\n"
        "    group_role_map:\n"
        "      eng-leads: [engineering]\n"
        "      sec-team: [security, qa]\n"
    )
    s = Settings.load(cfg)
    assert s.oidc_group_claim == "roles"
    assert s.group_role_map == {
        "eng-leads": ("engineering",),
        "sec-team": ("security", "qa"),
    }


def test_claim_names_from_env():
    s = Settings.load(
        env={
            "FOUNDRY_OIDC_ISSUER": ISSUER,
            "FOUNDRY_OIDC_AUDIENCE": AUDIENCE,
            "FOUNDRY_OIDC_JWKS_URI": "https://idp.example.com/jwks",
            "FOUNDRY_OIDC_SUBJECT_CLAIM": "preferred_username",
            "FOUNDRY_OIDC_GROUP_CLAIM": "roles",
        }
    )
    assert s.oidc_subject_claim == "preferred_username"
    assert s.oidc_group_claim == "roles"


def test_defaults_for_claims_and_empty_map():
    s = Settings.load()
    assert s.oidc_subject_claim == "email"
    assert s.oidc_group_claim == "groups"
    assert s.oidc_group_role_map == ()
    assert s.group_role_map == {}


# --------------------------------------------------------------------------- #
# REST approval bound to the verified OIDC token (issue #34)
# --------------------------------------------------------------------------- #

from foundry.api.security import compute_signature  # noqa: E402

WEBHOOK_SECRET = "wh-secret"
READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


def _approval_client(
    *,
    approvers=None,
    group_role_map=None,
    subject_claim="email",
    group_claim="groups",
    api_token=None,
):
    """An app with OIDC auth + Linear webhook intake wired for approval tests.

    Returns ``(client, priv)`` where ``priv`` is the RSA key whose public half
    the app's verifier trusts, so the test can mint tokens it will accept.
    """
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    priv, jwks = _keypair()
    client = TestClient(
        create_app(
            webhook_secret=WEBHOOK_SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers=approvers or {},
            api_token=api_token,
            oidc_verifier=_verifier(jwks),
            oidc_subject_claim=subject_claim,
            oidc_group_claim=group_claim,
            oidc_group_role_map=group_role_map or {},
        )
    )
    return client, priv


def _ready_payload(issue_id="issue-r", key="LIN-1", *, infra=False) -> dict:
    if infra:
        title = "Update the terraform deployment config"
        desc = "Acceptance Criteria:\n- terraform plan runs clean\n- it applies\n"
    else:
        title = "Add customer favourites"
        desc = READY_DESC
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": title,
            "description": desc,
            "labels": [{"name": "foundry:candidate"}, {"name": "repo:customer-web"}],
            "actor": {"name": "po@example.com"},
        }
    }


def _start_run(client, payload, *, delivery="d-1") -> str:
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + compute_signature(WEBHOOK_SECRET, body)
    resp = client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": delivery, "Linear-Signature": sig},
    )
    assert resp.json()["run"]["status"] == "waiting_approval", resp.json()
    return resp.json()["run"]["id"]


def test_oidc_approval_binds_actor_to_verified_subject():
    """The approver identity is the verified email claim, not the body 'user'."""
    client, priv = _approval_client(approvers={"alice@example.com": []})
    run_id = _start_run(client, _ready_payload())
    token = _token(priv, sub="alice@example.com", extra={"email": "alice@example.com"})
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},  # no body 'user' at all
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["run"]["approved_by"] == "alice@example.com"


def test_oidc_approval_refuses_body_user_mismatch():
    client, priv = _approval_client(approvers={"alice@example.com": ["engineering"]})
    run_id = _start_run(client, _ready_payload())
    token = _token(priv, extra={"email": "alice@example.com"})
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "mallory@example.com", "text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "does not match" in resp.json()["detail"]
    assert client.get(
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
    ).json()["status"] == "waiting_approval"


def test_oidc_group_grants_role_and_authorises_unlisted_user():
    """A verified member of a mapped IdP group gets the role and may approve a
    run requiring it - even though they are NOT in the static approvers list."""
    client, priv = _approval_client(
        approvers={},  # carol is not listed here
        group_role_map={"eng-leads": ["engineering"]},
    )
    run_id = _start_run(client, _ready_payload(infra=True))
    token = _token(
        priv,
        sub="carol@example.com",
        extra={"email": "carol@example.com", "groups": ["eng-leads"]},
    )
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["run"]["status"] == "agent_running"
    assert resp.json()["run"]["approved_by"] == "carol@example.com"


def test_oidc_group_role_insufficient_for_sensitive_run_is_refused():
    """A mapped group that grants the wrong role can't satisfy a run's required
    approval: the gate still refuses (issue #18 / invariant #1 unchanged)."""
    client, priv = _approval_client(
        approvers={},
        group_role_map={"qa-team": ["qa"]},  # qa != engineering
    )
    run_id = _start_run(client, _ready_payload(infra=True))
    token = _token(
        priv,
        sub="quinn@example.com",
        extra={"email": "quinn@example.com", "groups": ["qa-team"]},
    )
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "approval refused" in resp.json()["detail"]
    assert client.get(
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
    ).json()["status"] == "waiting_approval"


def test_oidc_user_without_standing_is_not_authorised():
    """A verified token whose subject is neither a configured approver nor in any
    mapped group cannot approve at all."""
    client, priv = _approval_client(
        approvers={"alice@example.com": []},
        group_role_map={"eng-leads": ["engineering"]},
    )
    run_id = _start_run(client, _ready_payload())
    token = _token(
        priv,
        sub="dave@example.com",
        extra={"email": "dave@example.com", "groups": ["randoms"]},
    )
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "not authorised" in resp.json()["detail"]


def test_oidc_token_without_subject_claim_refused():
    """A verified token carrying neither the configured subject claim nor a
    usable 'sub' cannot bind an approver identity -> 401."""
    client, priv = _approval_client(
        approvers={"alice@example.com": []}, subject_claim="email"
    )
    run_id = _start_run(client, _ready_payload())
    token = _token(priv, sub="", extra={})  # blank sub, no email
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert "subject claim" in resp.json()["detail"]


def test_static_token_path_ignores_group_map():
    """With a static token (no verified claims), behaviour is unchanged: the
    actor is the body 'user' and roles come from committed approvers - the
    IdP-group map plays no part."""
    client, _ = _approval_client(
        approvers={"lead@example.com": ["engineering"]},
        api_token="static-tok",
        group_role_map={"eng-leads": ["engineering"]},
    )
    run_id = _start_run(client, _ready_payload(infra=True))
    # Static-token caller approves as the body 'user', satisfied by config roles.
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers={"Authorization": "Bearer static-tok"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["run"]["status"] == "agent_running"
    assert resp.json()["run"]["approved_by"] == "lead@example.com"
