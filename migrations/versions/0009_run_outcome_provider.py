"""provider column on foundry_run_outcomes (agent scorecards)

Delivery memory already records *which repo* work landed in and what it cost;
agent scorecards need *which agent* shipped it. The provider lives on
``foundry_agent_jobs`` per dispatch; this denormalizes the latest dispatched
job's provider onto the outcome row (mirroring the existing ``repo`` column) so
per-provider success/retry/cost can be aggregated without re-joining the audit
trail on every request.

Nullable and additive: existing rows get NULL (never dispatched, or recorded
before this column existed) and ``foundry-memory backfill --recompute``
re-derives ``provider`` for terminal runs from their agent jobs.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-13 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'foundry_run_outcomes',
        sa.Column('provider', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('foundry_run_outcomes', 'provider')
