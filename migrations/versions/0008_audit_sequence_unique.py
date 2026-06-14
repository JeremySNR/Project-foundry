"""unique (run_id, sequence) on foundry_audit_events

The audit trail promises a guaranteed per-run order via the monotonic
``sequence`` column. That only holds if no two events for a run share a
sequence number. Before issue #10, ``sequence`` was assigned with an unlocked
``SELECT max(sequence)+1`` at flush time, so two concurrent sessions could
silently produce duplicates. State transitions now take a row lock, and this
unique index makes any remaining duplicate fail loudly at insert instead of
quietly corrupting the order.

NOTE: the upgrade fails if historical duplicate ``(run_id, sequence)`` pairs
already exist (the very corruption this prevents). Find them first, e.g.::

    SELECT run_id, sequence, COUNT(*) FROM foundry_audit_events
    GROUP BY run_id, sequence HAVING COUNT(*) > 1;

and renumber the offending rows before upgrading.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-13 00:00:00.000000

"""
from __future__ import annotations

from alembic import op


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'uq_audit_event_run_sequence',
        'foundry_audit_events',
        ['run_id', 'sequence'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        'uq_audit_event_run_sequence', table_name='foundry_audit_events'
    )
