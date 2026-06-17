"""SCIM 2.0 user/group provisioning tables (issue #157)

Adds the storage behind the SCIM ``/Users`` and ``/Groups`` provisioning surface
so an IdP can create/update/deactivate the identities and groups Foundry knows
about. The rows hold only identity + lifecycle state and group membership -
**never** a role grant: authority is still derived from the committed
``oidc_group_role_map`` (a SCIM group's ``displayName`` is the lookup key), so a
provisioned identity can never assert a role itself (invariant #5).

All three tables are tenant-scoped (``org_id``, like every table since #156) so a
multi-tenant deployment isolates one org's provisioned directory from another's.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-17 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

# Frozen here (migrations must not import application code); mirrors
# ``foundry.db.tenant.DEFAULT_ORG_ID``.
_DEFAULT_ORG_ID = 'default'


def upgrade() -> None:
    op.create_table(
        'foundry_scim_users',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('org_id', sa.String(length=64), nullable=False,
                  server_default=_DEFAULT_ORG_ID),
        sa.Column('user_name', sa.String(length=320), nullable=False),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', 'user_name', name='uq_scim_user_username'),
    )
    op.create_index('ix_foundry_scim_users_org_id', 'foundry_scim_users', ['org_id'])
    op.create_index(
        'ix_foundry_scim_users_external_id', 'foundry_scim_users', ['external_id']
    )

    op.create_table(
        'foundry_scim_groups',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('org_id', sa.String(length=64), nullable=False,
                  server_default=_DEFAULT_ORG_ID),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', 'display_name', name='uq_scim_group_displayname'),
    )
    op.create_index('ix_foundry_scim_groups_org_id', 'foundry_scim_groups', ['org_id'])
    op.create_index(
        'ix_foundry_scim_groups_external_id', 'foundry_scim_groups', ['external_id']
    )

    op.create_table(
        'foundry_scim_group_members',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('org_id', sa.String(length=64), nullable=False,
                  server_default=_DEFAULT_ORG_ID),
        sa.Column('group_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ['group_id'], ['foundry_scim_groups.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['foundry_scim_users.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'user_id', name='uq_scim_group_member'),
    )
    op.create_index(
        'ix_foundry_scim_group_members_org_id', 'foundry_scim_group_members', ['org_id']
    )
    op.create_index(
        'ix_foundry_scim_group_members_user', 'foundry_scim_group_members', ['user_id']
    )
    op.create_index(
        'ix_foundry_scim_group_members_group', 'foundry_scim_group_members', ['group_id']
    )


def downgrade() -> None:
    op.drop_table('foundry_scim_group_members')
    op.drop_index(
        'ix_foundry_scim_groups_external_id', table_name='foundry_scim_groups'
    )
    op.drop_index('ix_foundry_scim_groups_org_id', table_name='foundry_scim_groups')
    op.drop_table('foundry_scim_groups')
    op.drop_index(
        'ix_foundry_scim_users_external_id', table_name='foundry_scim_users'
    )
    op.drop_index('ix_foundry_scim_users_org_id', table_name='foundry_scim_users')
    op.drop_table('foundry_scim_users')
