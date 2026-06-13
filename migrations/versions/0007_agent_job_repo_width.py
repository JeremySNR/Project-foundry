"""widen foundry_agent_jobs.repo to 255

``FoundryAgentJob.repo`` was ``String(128)`` while every other ``repo`` column
(``foundry_run_outcomes``, ``foundry_repo_catalog``) is ``String(255)``. A long
``org/name`` that fits everywhere else could fail insert on Postgres mid-run.
Widen to 255 for parity.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-13 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('foundry_agent_jobs') as batch_op:
        batch_op.alter_column(
            'repo',
            existing_type=sa.String(128),
            type_=sa.String(255),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('foundry_agent_jobs') as batch_op:
        batch_op.alter_column(
            'repo',
            existing_type=sa.String(255),
            type_=sa.String(128),
            existing_nullable=True,
        )
