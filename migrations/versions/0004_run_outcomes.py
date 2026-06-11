"""run outcomes (delivery memory)

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-10 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'foundry_run_outcomes',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('linear_issue_id', sa.String(length=128), nullable=False),
        sa.Column('issue_key_prefix', sa.String(length=16), nullable=False),
        sa.Column('outcome', sa.String(length=32), nullable=False),
        sa.Column('repo', sa.String(length=255), nullable=True),
        sa.Column('routed_confidence', sa.Integer(), nullable=True),
        sa.Column('work_type', sa.String(length=32), nullable=True),
        sa.Column('labels_json', sa.Text(), nullable=False),
        sa.Column('risk_level', sa.String(length=32), nullable=True),
        sa.Column('agent_mode', sa.String(length=32), nullable=True),
        sa.Column('trigger_type', sa.String(length=64), nullable=False),
        sa.Column('created_at_run', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('time_to_merge_seconds', sa.Integer(), nullable=True),
        sa.Column('jobs_count', sa.Integer(), nullable=False),
        sa.Column('escalations_count', sa.Integer(), nullable=False),
        sa.Column('ci_failures_count', sa.Integer(), nullable=False),
        sa.Column('files_changed_count', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('blocked_reason_category', sa.String(length=32), nullable=True),
        sa.Column('block_justified', sa.Boolean(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['foundry_runs.id']),
        sa.PrimaryKeyConstraint('run_id'),
    )
    op.create_index(
        'idx_outcome_priors',
        'foundry_run_outcomes',
        ['issue_key_prefix', 'work_type', 'repo', 'outcome'],
    )
    op.create_index(
        'idx_outcome_completed', 'foundry_run_outcomes', ['completed_at']
    )
    op.create_index(
        op.f('ix_foundry_run_outcomes_linear_issue_id'),
        'foundry_run_outcomes',
        ['linear_issue_id'],
    )
    op.create_index(
        op.f('ix_foundry_run_outcomes_outcome'), 'foundry_run_outcomes', ['outcome']
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_foundry_run_outcomes_outcome'), table_name='foundry_run_outcomes')
    op.drop_index(
        op.f('ix_foundry_run_outcomes_linear_issue_id'),
        table_name='foundry_run_outcomes',
    )
    op.drop_index('idx_outcome_completed', table_name='foundry_run_outcomes')
    op.drop_index('idx_outcome_priors', table_name='foundry_run_outcomes')
    op.drop_table('foundry_run_outcomes')
