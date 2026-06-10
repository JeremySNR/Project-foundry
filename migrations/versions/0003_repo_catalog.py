"""repo catalog

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-10 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'foundry_repo_catalog',
        sa.Column('repo', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('topics', sa.Text(), nullable=False),
        sa.Column('primary_language', sa.String(length=64), nullable=True),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('default_branch', sa.String(length=128), nullable=True),
        sa.Column('readme_head', sa.Text(), nullable=True),
        sa.Column('top_dirs', sa.Text(), nullable=False),
        sa.Column('recent_pr_titles', sa.Text(), nullable=False),
        sa.Column('top_contributors', sa.Text(), nullable=False),
        sa.Column('pushed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('etag', sa.String(length=128), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('repo'),
    )


def downgrade() -> None:
    op.drop_table('foundry_repo_catalog')
