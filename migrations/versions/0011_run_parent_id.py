"""parent_run_id self-FK on foundry_runs (epic decomposition)

Issue #35 introduces a parent/child run model: an epic run decomposes into
child runs (one per repo / scope). This adds the self-referential
``parent_run_id`` column - a child points at the parent run it was split out
of - plus an index so listing an epic's children is a cheap lookup.

Nullable and additive: every existing run is a root (NULL parent), so the
column is backward compatible and needs no backfill.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-14 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'foundry_runs',
        sa.Column('parent_run_id', sa.String(length=64), nullable=True),
    )
    op.create_index(
        'ix_foundry_runs_parent_run_id', 'foundry_runs', ['parent_run_id']
    )
    op.create_foreign_key(
        'fk_foundry_runs_parent_run_id',
        'foundry_runs',
        'foundry_runs',
        ['parent_run_id'],
        ['id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_foundry_runs_parent_run_id', 'foundry_runs', type_='foreignkey'
    )
    op.drop_index('ix_foundry_runs_parent_run_id', table_name='foundry_runs')
    op.drop_column('foundry_runs', 'parent_run_id')
