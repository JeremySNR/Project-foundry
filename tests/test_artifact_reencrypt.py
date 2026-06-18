"""Re-wrapping artifact payloads under the current key (issue #163, #34 follow-up).

The cipher decrypts under any configured key but only ever *writes* under the
first (primary) key, and nothing re-wraps the bytes already on disk. These tests
exercise the offline re-wrap pass end-to-end against a real SQLite database:
legacy plaintext gets encrypted, ciphertext under a rotated-away key gets moved
onto the primary key, rows already current are left alone, and the plaintext
(hence content_hash) is preserved throughout.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from foundry.audit import build_artifact
from foundry.audit.events import _canonical
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
    reset_cipher,
)
from foundry.db.maintenance import reencrypt_artifacts
from foundry.schemas import TicketAnalysis

cryptography = pytest.importorskip("cryptography")
from cryptography.fernet import Fernet  # noqa: E402


def _key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture(autouse=True)
def _reset_cipher():
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


def _store(session, analysis: TicketAnalysis, run_id: str = "run-1") -> FoundryArtifact:
    run = FoundryRun(
        id=run_id,
        linear_issue_id="i",
        linear_issue_key="ENG-1",
        trigger_type="label",
    )
    art = build_artifact(
        run_id=run_id,
        artifact_type=ArtifactType.TICKET_ANALYSIS,
        content=analysis,
    )
    run.artifacts.append(art)
    session.add(run)
    session.commit()
    return art


def _raw(session, art_id: str) -> str:
    return session.execute(
        text("SELECT content_json FROM foundry_artifacts WHERE id = :id"),
        {"id": art_id},
    ).scalar_one()


# ----------------------------------------------------------- cipher primitives

def test_needs_reencrypt_flags_legacy_plaintext() -> None:
    cipher = build_cipher(_key())
    assert cipher.needs_reencrypt('{"legacy":true}') is True


def test_needs_reencrypt_skips_value_under_primary_key() -> None:
    cipher = build_cipher(_key())
    stored = cipher.encrypt('{"x":1}')
    assert cipher.needs_reencrypt(stored) is False


def test_needs_reencrypt_flags_rotated_away_key() -> None:
    old, new = _key(), _key()
    stored = build_cipher(old).encrypt('{"x":1}')
    # New primary first, old kept only for decrypt: the row is decryptable but
    # not under the primary key, so it must be re-wrapped.
    rotated = build_cipher(f"{new},{old}")
    assert rotated.needs_reencrypt(stored) is True


def test_reencrypt_moves_token_onto_primary_and_preserves_plaintext() -> None:
    old, new = _key(), _key()
    stored = build_cipher(old).encrypt("payload")
    rotated = build_cipher(f"{new},{old}")

    rewrapped = rotated.reencrypt(stored)
    # Plaintext preserved...
    assert rotated.decrypt(rewrapped) == "payload"
    # ...and now readable under the new key alone (the old key can be retired).
    assert build_cipher(new).decrypt(rewrapped) == "payload"
    assert rotated.needs_reencrypt(rewrapped) is False


def test_null_cipher_never_needs_reencrypt() -> None:
    assert NullCipher().needs_reencrypt('{"x":1}') is False


# ------------------------------------------------------------- maintenance pass

def test_reencrypt_backfills_legacy_plaintext_rows(
    session, ready_analysis: TicketAnalysis
) -> None:
    # Written while disabled => legacy plaintext on disk.
    art = _store(session, ready_analysis)
    plaintext = _canonical(ready_analysis)
    assert not _raw(session, art.id).startswith(encryption._PREFIX)

    key = _key()
    cipher = configure_cipher_from_key(key)  # process cipher + the pass's cipher
    report = reencrypt_artifacts(session, cipher)
    assert (report.scanned, report.rewrapped, report.skipped, report.failed) == (
        1,
        1,
        0,
        0,
    )

    # Now ciphertext at rest, plaintext unchanged, content_hash untouched.
    raw = _raw(session, art.id)
    assert raw.startswith(encryption._PREFIX)
    assert ready_analysis.title not in raw
    session.expire_all()
    fetched = session.get(FoundryArtifact, art.id)
    assert fetched.content_json == plaintext
    assert fetched.content_hash == hashlib.sha256(
        plaintext.encode("utf-8")
    ).hexdigest()


def test_reencrypt_rewraps_rotated_away_rows_so_old_key_can_retire(
    session, ready_analysis: TicketAnalysis
) -> None:
    old = _key()
    # A row written under the old key (the historical primary).
    configure_cipher_from_key(old)
    art = _store(session, ready_analysis)
    assert _raw(session, art.id).startswith(encryption._PREFIX)

    # Rotate: new key primary, old kept only so the pass can still decrypt.
    new = _key()
    cipher = configure_cipher_from_key(f"{new},{old}")
    report = reencrypt_artifacts(session, cipher)
    assert report.rewrapped == 1
    assert report.failed == 0

    # The stored bytes now decrypt under the *new* key alone: the old key is
    # free to be dropped from the configuration.
    plaintext = _canonical(ready_analysis)
    assert build_cipher(new).decrypt(_raw(session, art.id)) == plaintext


def test_reencrypt_is_idempotent_and_skips_current_rows(
    session, ready_analysis: TicketAnalysis
) -> None:
    key = _key()
    cipher = configure_cipher_from_key(key)
    _store(session, ready_analysis)  # already written under the primary key

    first = reencrypt_artifacts(session, cipher)
    assert (first.rewrapped, first.skipped) == (0, 1)

    # A row backfilled on a prior run is now current; a second pass is a no-op.
    second = reencrypt_artifacts(session, cipher)
    assert (second.rewrapped, second.skipped) == (0, 1)


def test_dry_run_reports_without_writing(
    session, ready_analysis: TicketAnalysis
) -> None:
    art = _store(session, ready_analysis)  # legacy plaintext
    cipher = configure_cipher_from_key(_key())

    report = reencrypt_artifacts(session, cipher, dry_run=True)
    assert report.rewrapped == 1
    # Nothing was actually written: still plaintext on disk.
    assert not _raw(session, art.id).startswith(encryption._PREFIX)


def test_reencrypt_spans_all_orgs(
    session, ready_analysis: TicketAnalysis
) -> None:
    # Rows under a non-default org must be re-wrapped too: the maintenance pass
    # uses Core SQL and is not constrained by the per-org ORM filter.
    from foundry.db.tenant import tenant_context

    art = _store(session, ready_analysis, run_id="run-default")
    with tenant_context("tenant-x"):
        art_x = _store(session, ready_analysis, run_id="run-tenant-x")

    cipher = configure_cipher_from_key(_key())
    report = reencrypt_artifacts(session, cipher)
    assert report.scanned == 2
    assert report.rewrapped == 2
    assert _raw(session, art.id).startswith(encryption._PREFIX)
    assert _raw(session, art_x.id).startswith(encryption._PREFIX)


# --------------------------------------------------------------------- the CLI


def test_cli_refuses_without_key(monkeypatch, capsys, tmp_path) -> None:
    from foundry.db.cli import main

    db_url = f"sqlite+pysqlite:///{tmp_path}/foundry.db"
    create_all(make_engine(db_url))
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.delenv("FOUNDRY_ARTIFACT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)

    with pytest.raises(SystemExit) as exc:
        main(["reencrypt-artifacts"])
    assert exc.value.code == 1
    assert "nothing to re-encrypt" in capsys.readouterr().err


def test_cli_reencrypts_legacy_rows(
    monkeypatch, capsys, tmp_path, ready_analysis: TicketAnalysis
) -> None:
    from foundry.db.cli import main

    db_url = f"sqlite+pysqlite:///{tmp_path}/foundry.db"
    # Seed a legacy-plaintext row (process cipher null at write time).
    reset_cipher()
    factory = make_session_factory(make_engine(db_url))
    create_all(make_engine(db_url))
    with factory() as s:
        art = _store(s, ready_analysis)
        art_id = art.id
        assert not _raw(s, art_id).startswith(encryption._PREFIX)

    key = _key()
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setenv("FOUNDRY_ARTIFACT_ENCRYPTION_KEY", key)
    main(["reencrypt-artifacts"])

    out = capsys.readouterr().out
    assert "1 re-wrapped" in out

    # The row is now encrypted under the configured key.
    with factory() as s:
        raw = _raw(s, art_id)
    assert raw.startswith(encryption._PREFIX)
    assert build_cipher(key).decrypt(raw) == _canonical(ready_analysis)


def test_cli_dry_run_writes_nothing(
    monkeypatch, capsys, tmp_path, ready_analysis: TicketAnalysis
) -> None:
    from foundry.db.cli import main

    db_url = f"sqlite+pysqlite:///{tmp_path}/foundry.db"
    reset_cipher()
    factory = make_session_factory(make_engine(db_url))
    create_all(make_engine(db_url))
    with factory() as s:
        art_id = _store(s, ready_analysis).id

    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setenv("FOUNDRY_ARTIFACT_ENCRYPTION_KEY", _key())
    main(["reencrypt-artifacts", "--dry-run"])

    assert "would be re-wrapped" in capsys.readouterr().out
    with factory() as s:
        assert not _raw(s, art_id).startswith(encryption._PREFIX)
