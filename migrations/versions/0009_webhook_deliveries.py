"""durable webhook delivery dedup

Replaces the in-process dedup ``set`` (per-process, lost on restart, unbounded)
with a durable, bounded table. The unique ``(provider, delivery_id)`` makes
dedup atomic across workers; ``received_at`` is indexed for TTL pruning.

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
    op.create_table(
        'foundry_webhook_deliveries',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('delivery_id', sa.String(length=255), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'delivery_id', name='uq_webhook_delivery'),
    )
    op.create_index(
        'idx_webhook_delivery_received',
        'foundry_webhook_deliveries',
        ['received_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'idx_webhook_delivery_received',
        table_name='foundry_webhook_deliveries',
    )
    op.drop_table('foundry_webhook_deliveries')
