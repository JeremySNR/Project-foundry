"""org_id on all tenant-scoped tables + row-level isolation (issue #156)

Foundry's data model was single-tenant: every read and write assumed one tenant.
This adds an ``org_id`` to every tenant-scoped table so a deployment can serve
multiple orgs with row-level isolation (enforced at the query layer by the
session machinery in ``foundry.db.base`` — a ``with_loader_criteria`` filter and
a flush-time stamp, both reading the active tenant context).

The column is ``NOT NULL`` with a ``server_default`` of ``'default'``, so adding
it **backfills every existing row to the default org** in one statement — a
single-tenant database keeps working unchanged, all its rows under that one org.
Each column is indexed because every tenant-scoped read filters on it.

The "one active run per issue" partial unique index is recreated scoped by
``org_id`` so two tenants can each have an active run for the same upstream issue
id; within one org the constraint is identical, so a single-tenant deployment is
unaffected.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-17 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

# Frozen here (migrations must not import application code); mirrors
# ``foundry.db.tenant.DEFAULT_ORG_ID``.
_DEFAULT_ORG_ID = 'default'

# Every tenant-scoped table. Mirrors ``foundry.db.models.TENANT_SCOPED_MODELS``.
_TENANT_TABLES = (
    'foundry_runs',
    'foundry_artifacts',
    'foundry_audit_events',
    'foundry_policy_decisions',
    'foundry_agent_jobs',
    'foundry_run_outcomes',
    'foundry_webhook_deliveries',
    'foundry_repo_catalog',
)

# Frozen copy of ACTIVE_RUN_STATUSES (enum *names*), as in migration 0006.
_ACTIVE_STATUS_PREDICATE = (
    "status IN ('AGENT_RUNNING', 'ANALYSING', 'APPROVED', 'PLAN_READY', "
    "'PR_OPEN', 'REVIEW_REQUIRED', 'WAITING_APPROVAL')"
)


def upgrade() -> None:
    for table in _TENANT_TABLES:
        # NOT NULL + server_default backfills every existing row to the default
        # org in the same statement.
        op.add_column(
            table,
            sa.Column(
                'org_id',
                sa.String(length=64),
                nullable=False,
                server_default=_DEFAULT_ORG_ID,
            ),
        )
        op.create_index(f'ix_{table}_org_id', table, ['org_id'])

    # Re-scope the one-active-run-per-issue uniqueness by org.
    op.drop_index(
        'uq_foundry_runs_one_active_per_issue', table_name='foundry_runs'
    )
    op.create_index(
        'uq_foundry_runs_one_active_per_issue',
        'foundry_runs',
        ['org_id', 'linear_issue_id'],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATUS_PREDICATE),
        sqlite_where=sa.text(_ACTIVE_STATUS_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_foundry_runs_one_active_per_issue', table_name='foundry_runs'
    )
    op.create_index(
        'uq_foundry_runs_one_active_per_issue',
        'foundry_runs',
        ['linear_issue_id'],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATUS_PREDICATE),
        sqlite_where=sa.text(_ACTIVE_STATUS_PREDICATE),
    )
    for table in _TENANT_TABLES:
        op.drop_index(f'ix_{table}_org_id', table_name=table)
        op.drop_column(table, 'org_id')
