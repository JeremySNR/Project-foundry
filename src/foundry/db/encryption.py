"""Transparent application-layer encryption for artifact payloads at rest.

Foundry stores every run artifact's content as canonical JSON in
``foundry_artifacts.content_json``. That payload carries ticket text, plans, and
diffs - data a buyer's security review wants encrypted at rest. This module
provides an opt-in, transparent cipher applied at the SQLAlchemy column boundary
(:class:`EncryptedText`): the application always reads and writes plaintext,
while the bytes on disk are ciphertext when a key is configured.

Design choices that keep the rest of the system untouched:

* **Hash over plaintext, not ciphertext.** Artifact content hashing
  (``audit/events.py``) and the compliance evidence-pack integrity check verify
  the *plaintext* canonical JSON. Encryption is a storage transform only, so
  those hashes - and dedup by ``content_hash`` - are unaffected whether or not a
  key is set.
* **No schema change.** The on-disk SQL type stays ``TEXT``; we only widen the
  string it holds. So there is no Alembic migration and existing databases keep
  working.
* **Backward / forward compatible.** Every ciphertext carries a version prefix
  (:data:`_PREFIX`). A row written before a key was configured has no prefix and
  is read back verbatim, so enabling encryption on an existing database is safe.
  Conversely, :class:`NullCipher` (no key) refuses to silently hand back a
  ciphertext it cannot decrypt - it raises, so a removed/rotated-away key fails
  loud instead of corrupting reads.
* **Opt-in, secret from env.** The key lives in
  ``FOUNDRY_ARTIFACT_ENCRYPTION_KEY`` (never YAML), like every other secret.
  Unset => :class:`NullCipher` => byte-for-byte the previous plaintext behaviour.
  Comma-separated keys enable rotation (first key encrypts; all are tried for
  decrypt) via ``cryptography``'s ``MultiFernet``.

Real symmetric encryption needs a cipher implementation; the standard library
has none, so :class:`FernetArtifactCipher` uses ``cryptography`` (the optional
``crypto`` extra). It is imported lazily so the no-key path - and the offline
core test suite - never require the dependency.
"""

from __future__ import annotations

import os

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

# Marks a stored value as an encrypted token. Versioned so the on-disk format
# can evolve without ambiguity; the absence of this prefix means "legacy
# plaintext, written before a key was configured".
_PREFIX = "fdyenc:1:"
ENV_KEY = "FOUNDRY_ARTIFACT_ENCRYPTION_KEY"


class ArtifactCipher:
    """Encrypt/decrypt a text payload at the storage boundary."""

    enabled: bool = False

    def encrypt(self, plaintext: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def decrypt(self, stored: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def needs_reencrypt(self, stored: str) -> bool:
        """Whether ``stored`` should be re-wrapped under the current primary key.

        ``True`` for a value not yet protected by the active primary key - legacy
        plaintext written before a key was configured, or ciphertext under a
        rotated-away key. ``False`` (the safe default) means "leave it alone": no
        cipher, or already under the primary key. Used by the offline re-wrap
        maintenance pass (``db/maintenance.py``).
        """
        return False

    def reencrypt(self, stored: str) -> str:
        """Re-wrap ``stored`` under the current primary key, preserving plaintext.

        Decrypts with whichever configured key works and re-encrypts the *same*
        plaintext, so the plaintext content hash is untouched. The no-cipher base
        is a pass-through.
        """
        return stored


class NullCipher(ArtifactCipher):
    """No key configured: store and return plaintext (the historical behaviour).

    Reading a value that *is* encrypted with no key to decrypt it is a
    misconfiguration (the key was removed or rotated away), so we fail loud
    rather than hand back ciphertext that downstream JSON parsing would choke on
    or, worse, store again.
    """

    enabled = False

    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, stored: str) -> str:
        if stored.startswith(_PREFIX):
            raise RuntimeError(
                "artifact payload is encrypted but no "
                f"{ENV_KEY} is configured to decrypt it "
                "(was the key removed or rotated away?)"
            )
        return stored


class FernetArtifactCipher(ArtifactCipher):
    """Authenticated symmetric encryption via ``cryptography``'s Fernet.

    ``keys`` may hold more than one key for rotation: the first encrypts, all
    are tried in order for decrypt (``MultiFernet``).
    """

    enabled = True

    def __init__(self, keys: list[str]) -> None:
        # Imported lazily: the no-key path never needs ``cryptography``.
        from cryptography.fernet import Fernet, MultiFernet

        fernets = [Fernet(k.encode("ascii")) for k in keys]
        self._fernet = MultiFernet(fernets)
        # The first key encrypts new writes; keep it on its own so
        # ``needs_reencrypt`` can tell "already under the primary key" from
        # "encrypted under a rotated-away key" - MultiFernet can't, it tries all.
        self._primary = fernets[0]

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return _PREFIX + token

    def decrypt(self, stored: str) -> str:
        if not stored.startswith(_PREFIX):
            # Legacy plaintext written before encryption was enabled - readable
            # as-is, so turning a key on for an existing database is safe.
            return stored
        token = stored[len(_PREFIX):]
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    def needs_reencrypt(self, stored: str) -> bool:
        if not stored.startswith(_PREFIX):
            # Legacy plaintext: readable, but not actually encrypted at rest -
            # re-wrap it now that a key is configured.
            return True
        from cryptography.fernet import InvalidToken

        token = stored[len(_PREFIX):].encode("ascii")
        try:
            self._primary.decrypt(token)
        except InvalidToken:
            # Decryptable by the MultiFernet (some configured key) but not by the
            # primary alone => encrypted under a rotated-away key. Re-wrap it so
            # that old key can finally be retired.
            return True
        return False

    def reencrypt(self, stored: str) -> str:
        return self.encrypt(self.decrypt(stored))


def build_cipher(key: str | None) -> ArtifactCipher:
    """Build the cipher for a configured key (``None``/empty => :class:`NullCipher`).

    Raises a clear error when a key is set but ``cryptography`` is missing or the
    key material is invalid, so misconfiguration fails closed at startup rather
    than at the first write.
    """
    keys = [part.strip() for part in (key or "").split(",") if part.strip()]
    if not keys:
        return NullCipher()
    try:
        return FernetArtifactCipher(keys)
    except ImportError as exc:  # cryptography not installed
        raise RuntimeError(
            f"{ENV_KEY} is set but the 'cryptography' package is not installed; "
            "install project-foundry[crypto] to enable artifact encryption at rest"
        ) from exc
    except Exception as exc:  # invalid Fernet key material
        raise ValueError(
            f"{ENV_KEY} is not a valid Fernet key (expected a url-safe "
            "base64-encoded 32-byte key, or several comma-separated for rotation)"
        ) from exc


# Process-wide cipher. Lazily built from the environment on first use so every
# entry point (API, CLIs, ad-hoc orchestrator construction) picks the key up
# without explicit wiring; entry points that hold Settings call
# ``configure_cipher_from_key`` to set it deterministically at startup.
_cipher: ArtifactCipher | None = None


def configure_cipher_from_key(key: str | None) -> ArtifactCipher:
    """Set the process cipher from a configured key (called at startup)."""
    global _cipher
    _cipher = build_cipher(key)
    return _cipher


def get_cipher() -> ArtifactCipher:
    """Return the process cipher, lazily building it from the environment."""
    global _cipher
    if _cipher is None:
        _cipher = build_cipher(os.environ.get(ENV_KEY))
    return _cipher


def reset_cipher() -> None:
    """Forget the configured cipher (re-derived from the environment next use).

    Used by tests so a key set in one test cannot leak into the next.
    """
    global _cipher
    _cipher = None


class EncryptedText(TypeDecorator):
    """A ``TEXT`` column whose value is encrypted at rest when a key is configured.

    Transparent: callers read and write plaintext; the process cipher (see
    :func:`get_cipher`) transforms it at the database boundary. The underlying
    SQL type is unchanged (``TEXT``), so no migration is required, and the
    content hash stored alongside the payload - computed over plaintext - is
    unaffected by encryption.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # value -> stored
        if value is None:
            return None
        return get_cipher().encrypt(value)

    def process_result_value(self, value, dialect):  # stored -> value
        if value is None:
            return None
        return get_cipher().decrypt(value)
