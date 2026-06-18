"""Offline maintenance over encrypted artifact payloads (issue #163, a #34 follow-up).

The artifact cipher (``db/encryption.py``) supports key rotation *for reads* -
``MultiFernet`` tries every configured key to decrypt and the first key encrypts
new writes - but nothing ever re-wraps the bytes already on disk. So two
operationally important states never resolve on their own:

* a key turned on for an existing database leaves every pre-existing row as
  legacy plaintext *forever* (readable, but never actually encrypted at rest), and
* after rotating to a new primary key, old rows stay encrypted under the
  *retired* key, so that key can never be dropped from
  ``FOUNDRY_ARTIFACT_ENCRYPTION_KEY`` - removing it would make those rows
  undecryptable.

:func:`reencrypt_artifacts` walks ``foundry_artifacts`` and re-wraps every row
that is not already under the current primary key (legacy plaintext, or
ciphertext under a rotated-away key), so the retired key can finally be retired.
It is:

* **plaintext-preserving** - it decrypts with whichever configured key works and
  re-encrypts the *same* plaintext, so ``content_hash`` (computed over plaintext)
  and evidence-pack integrity verification are untouched. It verifies the
  decrypt round-trip *before* overwriting a row, so a row it cannot read back is
  never clobbered.
* **all-orgs** - it uses Core SQL, which bypasses both the ``EncryptedText``
  column transform (so it sees the raw stored bytes) and the per-org ORM filter
  (so a single pass covers every tenant). This is a privileged operator task.
* **idempotent** - rows already under the primary key are detected and skipped,
  so a second run rewrites nothing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import text

from .encryption import ArtifactCipher


@dataclass
class ReencryptReport:
    """Outcome of a re-wrap pass."""

    scanned: int = 0
    rewrapped: int = 0
    skipped: int = 0
    failed: int = 0


def reencrypt_artifacts(
    session,
    cipher: ArtifactCipher,
    *,
    dry_run: bool = False,
) -> ReencryptReport:
    """Re-wrap every artifact row not already under the primary key.

    ``session`` is any SQLAlchemy session bound to the Foundry schema; ``cipher``
    is the configured artifact cipher (see :func:`foundry.db.encryption.get_cipher`).
    Returns a :class:`ReencryptReport`. With ``dry_run`` the rows that *would* be
    re-wrapped are counted but nothing is written.
    """
    report = ReencryptReport()
    rows = session.execute(
        text("SELECT id, content_json FROM foundry_artifacts")
    ).all()
    for row in rows:
        report.scanned += 1
        stored = row.content_json
        if stored is None or not cipher.needs_reencrypt(stored):
            report.skipped += 1
            continue
        try:
            plaintext = cipher.decrypt(stored)
            new_value = cipher.encrypt(plaintext)
            # Never overwrite a row we cannot read back: the re-wrapped value
            # must decrypt to the exact same plaintext, so content_hash and
            # integrity verification stay valid.
            if cipher.decrypt(new_value) != plaintext:
                raise RuntimeError("re-wrap round-trip did not preserve plaintext")
        except Exception as exc:  # noqa: BLE001 - report and continue, never clobber
            report.failed += 1
            print(f"warning: artifact {row.id}: {exc}", file=sys.stderr)
            continue
        if not dry_run:
            session.execute(
                text(
                    "UPDATE foundry_artifacts SET content_json = :value "
                    "WHERE id = :id"
                ),
                {"value": new_value, "id": row.id},
            )
        report.rewrapped += 1
    if not dry_run:
        session.commit()
    return report
