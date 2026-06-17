"""Artifact payload encryption at rest (issue #34).

The cipher is exercised end-to-end against a real SQLite database so we can
assert the on-disk bytes are ciphertext while the ORM hands callers plaintext,
and that the content hash (computed over plaintext) is unaffected.

``cryptography`` is the optional ``[crypto]`` extra; CI installs it via the
``[test]`` extra. The encrypted-path tests skip gracefully where it is absent so
the offline core suite stays green with no extra dependency.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from foundry.audit import build_artifact
from foundry.audit.events import _canonical
from foundry.config import Settings
from foundry.db import (
    ArtifactType,
    FoundryArtifact,
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db import encryption
from foundry.db.encryption import (
    NullCipher,
    build_cipher,
    configure_cipher_from_key,
    get_cipher,
    reset_cipher,
)
from foundry.schemas import TicketAnalysis

cryptography = pytest.importorskip("cryptography")
from cryptography.fernet import Fernet  # noqa: E402


def _key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture(autouse=True)
def _reset_cipher():
    """A key set in one test must never leak into the next."""
    reset_cipher()
    yield
    reset_cipher()


@pytest.fixture
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


def _store_analysis(session, analysis: TicketAnalysis) -> FoundryArtifact:
    run = FoundryRun(
        id="run-enc",
        linear_issue_id="i",
        linear_issue_key="ENG-1",
        trigger_type="label",
    )
    art = build_artifact(
        run_id="run-enc",
        artifact_type=ArtifactType.TICKET_ANALYSIS,
        content=analysis,
    )
    run.artifacts.append(art)
    session.add(run)
    session.commit()
    return art


def _raw_content_json(session) -> str:
    """The stored bytes, bypassing the column's decrypt-on-read processor."""
    return session.execute(
        text("SELECT content_json FROM foundry_artifacts")
    ).scalar_one()


# --------------------------------------------------------------- cipher unit

def test_build_cipher_without_key_is_null_passthrough() -> None:
    for key in (None, "", "   ", ",  ,"):
        cipher = build_cipher(key)
        assert isinstance(cipher, NullCipher)
        assert cipher.enabled is False
        assert cipher.encrypt("hello") == "hello"
        assert cipher.decrypt("hello") == "hello"


def test_null_cipher_refuses_to_read_ciphertext() -> None:
    # A removed/rotated-away key must fail loud, not hand back ciphertext.
    real = build_cipher(_key())
    token = real.encrypt("secret")
    with pytest.raises(RuntimeError):
        NullCipher().decrypt(token)


def test_fernet_cipher_roundtrip_and_prefix() -> None:
    cipher = build_cipher(_key())
    assert cipher.enabled is True
    plaintext = '{"ticket":"ENG-1","summary":"sensitive"}'
    stored = cipher.encrypt(plaintext)
    assert stored.startswith(encryption._PREFIX)
    assert "sensitive" not in stored
    assert cipher.decrypt(stored) == plaintext


def test_fernet_cipher_reads_legacy_plaintext_verbatim() -> None:
    # Rows written before a key was configured carry no prefix and must read
    # back as-is, so enabling encryption on an existing database is safe.
    cipher = build_cipher(_key())
    assert cipher.decrypt('{"legacy":true}') == '{"legacy":true}'


def test_invalid_key_raises() -> None:
    with pytest.raises(ValueError):
        build_cipher("not-a-valid-fernet-key")


def test_key_rotation_decrypts_old_token_and_encrypts_with_new() -> None:
    old, new = _key(), _key()
    old_cipher = build_cipher(old)
    token = old_cipher.encrypt("payload")

    # New primary first, old kept for decrypt (MultiFernet order).
    rotated = build_cipher(f"{new},{old}")
    assert rotated.decrypt(token) == "payload"

    fresh = rotated.encrypt("payload")
    # The new primary encrypts; a cipher holding only the new key can read it,
    # but one holding only the old key cannot.
    assert build_cipher(new).decrypt(fresh) == "payload"
    with pytest.raises(Exception):
        build_cipher(old).decrypt(fresh)


# --------------------------------------------------- column behaviour (DB)

def test_payload_encrypted_at_rest_but_plaintext_to_callers(
    session, ready_analysis: TicketAnalysis
) -> None:
    configure_cipher_from_key(_key())
    art = _store_analysis(session, ready_analysis)
    plaintext = _canonical(ready_analysis)

    # On disk: ciphertext, no plaintext leakage.
    raw = _raw_content_json(session)
    assert raw.startswith(encryption._PREFIX)
    assert ready_analysis.title not in raw

    # To callers (ORM read): plaintext canonical JSON, transparently decrypted.
    session.expire_all()
    fetched = session.get(FoundryArtifact, art.id)
    assert fetched.content_json == plaintext
    assert TicketAnalysis.model_validate_json(fetched.content_json).title == (
        ready_analysis.title
    )


def test_content_hash_is_over_plaintext_so_verification_holds(
    session, ready_analysis: TicketAnalysis
) -> None:
    configure_cipher_from_key(_key())
    art = _store_analysis(session, ready_analysis)
    plaintext = _canonical(ready_analysis)

    # The hash matches the plaintext, not the stored ciphertext - so the
    # evidence-pack integrity check and dedup by content_hash are unaffected.
    assert art.content_hash == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    session.expire_all()
    fetched = session.get(FoundryArtifact, art.id)
    assert (
        hashlib.sha256(fetched.content_json.encode("utf-8")).hexdigest()
        == fetched.content_hash
    )


def test_no_key_stores_plaintext_unchanged(
    session, ready_analysis: TicketAnalysis
) -> None:
    # Default (no key) is byte-for-byte the historical behaviour: plaintext on
    # disk, no prefix.
    assert get_cipher().enabled is False
    _store_analysis(session, ready_analysis)
    raw = _raw_content_json(session)
    assert not raw.startswith(encryption._PREFIX)
    assert raw == _canonical(ready_analysis)


def test_enabling_key_later_still_reads_old_plaintext_rows(
    session, ready_analysis: TicketAnalysis
) -> None:
    # Write while disabled (legacy plaintext)...
    art = _store_analysis(session, ready_analysis)
    # ...then turn a key on and read the pre-existing row back.
    configure_cipher_from_key(_key())
    session.expire_all()
    fetched = session.get(FoundryArtifact, art.id)
    assert fetched.content_json == _canonical(ready_analysis)


def test_removing_key_after_encrypting_fails_loud(
    session, ready_analysis: TicketAnalysis
) -> None:
    configure_cipher_from_key(_key())
    art = _store_analysis(session, ready_analysis)
    # Key removed (e.g. env unset / misconfig): reading encrypted bytes must
    # raise, never silently corrupt.
    configure_cipher_from_key(None)
    session.expire_all()
    with pytest.raises(RuntimeError):
        _ = session.get(FoundryArtifact, art.id).content_json


# ------------------------------------------------------------------ config

def test_settings_reads_key_from_env() -> None:
    key = _key()
    s = Settings.from_env({"FOUNDRY_ARTIFACT_ENCRYPTION_KEY": key})
    assert s.artifact_encryption_key == key


def test_settings_default_key_is_none() -> None:
    assert Settings.from_env({}).artifact_encryption_key is None


def test_settings_rejects_invalid_key_at_load() -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"FOUNDRY_ARTIFACT_ENCRYPTION_KEY": "bogus"})
