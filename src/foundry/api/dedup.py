"""Durable, bounded webhook deduplication + replay-age validation.

Two replay defences live here:

- :class:`WebhookDeduplicator` records every inbound delivery id in the
  ``foundry_webhook_deliveries`` table. A second delivery with the same
  ``(provider, delivery_id)`` is rejected as a duplicate. This is atomic
  (unique constraint, so concurrent workers can't both process it), durable
  (survives a restart), and bounded (rows past the TTL are pruned). It
  replaces the old in-process ``set`` that had none of those properties.

- :func:`webhook_timestamp_fresh` validates a provider-supplied timestamp
  (Linear's ``webhookTimestamp``) against a configured maximum age. A captured
  delivery that is signed correctly still fails once it is older than the
  window, so it cannot be replayed indefinitely. Fail-closed: a missing or
  unparseable timestamp is treated as stale when the check is enabled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from foundry.db.models import FoundryWebhookDelivery


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WebhookDeduplicator:
    """DB-backed idempotency guard for inbound webhook deliveries."""

    def __init__(
        self,
        session_factory,
        *,
        ttl_seconds: int | None = 86_400,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ttl_seconds = ttl_seconds
        self._clock = clock or _utcnow

    def seen(self, provider: str, delivery_id: str | None) -> bool:
        """Record ``delivery_id`` for ``provider``; return ``True`` if it was
        already recorded (i.e. this is a replay/redelivery).

        A missing delivery id cannot be deduplicated, so it is always treated
        as new - the one-active-run-per-issue guard remains the backstop for
        intake, and unsigned/idless deliveries never reach here in production.
        """
        if not delivery_id:
            return False
        now = self._clock()
        with self._session_factory() as session:
            self._prune(session, now)
            session.add(
                FoundryWebhookDelivery(
                    id=uuid4().hex,
                    provider=provider,
                    delivery_id=delivery_id,
                    received_at=now,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                # Lost the race (or a genuine redelivery): the row already
                # exists. Roll back our insert and report the duplicate.
                session.rollback()
                return True
        return False

    def _prune(self, session, now: datetime) -> None:
        """Delete deliveries older than the TTL so the table stays bounded."""
        if self._ttl_seconds is None:
            return
        cutoff = now - timedelta(seconds=self._ttl_seconds)
        session.execute(
            delete(FoundryWebhookDelivery).where(
                FoundryWebhookDelivery.received_at < cutoff
            )
        )


def webhook_timestamp_fresh(
    timestamp_ms: object,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    """True iff a provider webhook timestamp (epoch milliseconds, as Linear
    sends in ``webhookTimestamp``) is within ``max_age_seconds`` of ``now``.

    Fail-closed: a missing, non-numeric, or out-of-range timestamp returns
    ``False``. A symmetric window is allowed in the future too, so benign clock
    skew doesn't reject live deliveries while still bounding replay age.
    """
    if timestamp_ms is None:
        return False
    try:
        ts = datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    delta = (now - ts).total_seconds()
    return -max_age_seconds <= delta <= max_age_seconds
