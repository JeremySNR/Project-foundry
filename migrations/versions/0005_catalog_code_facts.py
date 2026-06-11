"""catalog code facts

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-11 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'foundry_repo_catalog',
        sa.Column('tree_paths', sa.Text(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        'foundry_repo_catalog',
        sa.Column('tree_truncated', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'foundry_repo_catalog',
        sa.Column('test_layout', sa.Text(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        'foundry_repo_catalog',
        sa.Column('codeowners', sa.Text(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        'foundry_repo_catalog',
        sa.Column('manifests', sa.Text(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        'foundry_repo_catalog',
        sa.Column('languages', sa.Text(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column('foundry_repo_catalog', 'languages')
    op.drop_column('foundry_repo_catalog', 'manifests')
    op.drop_column('foundry_repo_catalog', 'codeowners')
    op.drop_column('foundry_repo_catalog', 'test_layout')
    op.drop_column('foundry_repo_catalog', 'tree_truncated')
    op.drop_column('foundry_repo_catalog', 'tree_paths')
