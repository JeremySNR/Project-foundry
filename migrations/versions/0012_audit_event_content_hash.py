"""content_hash chain column on foundry_audit_events

Issue #36 upgrades the audit trail's integrity guarantee from "every artifact's
own hash matches + the per-run sequence is gap-free" to a genuine cross-row
linked hash chain: each audit event's ``content_hash`` commits to the previous
event's hash for the run, so dropping, reordering, or editing any row is
detectable on verification.

This adds the ``content_hash`` column. Nullable and additive: rows written
before the chain existed keep a NULL hash and are reported as un-chained (rather
than failing verification), so enabling the chain on an existing database is
safe and needs no backfill. New events written after this migration are chained
automatically by the flush hook in ``foundry.db.base``.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-15 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'foundry_audit_events',
        sa.Column('content_hash', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('foundry_audit_events', 'content_hash')
