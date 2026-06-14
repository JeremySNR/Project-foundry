"""one active run per issue (partial unique index)

Closes the intake race: two simultaneous webhook deliveries for the same issue
could both pass the application-level "active run?" check and create two runs.
The partial unique index makes the database the arbiter - at most one run per
``linear_issue_id`` may sit in an in-flight status (finished runs are exempt,
so re-analysis after clarification/rejection/failure still works).

Statuses are persisted by enum *name* and the list mirrors
``foundry.schemas.common.ACTIVE_RUN_STATUSES`` (frozen here, as migrations
must not import application code).

NOTE: the upgrade fails if historical duplicate active runs already exist for
one issue (the very bug this fixes). Resolve them first, e.g.::

    SELECT linear_issue_id, COUNT(*) FROM foundry_runs
    WHERE status IN ('AGENT_RUNNING', 'ANALYSING', 'APPROVED', 'PLAN_READY',
                     'PR_OPEN', 'REVIEW_REQUIRED', 'WAITING_APPROVAL')
    GROUP BY linear_issue_id HAVING COUNT(*) > 1;

then stop/reject the stale duplicates through the API so the terminal
transition is audited like any other.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-13 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

_ACTIVE_STATUS_PREDICATE = (
    "status IN ('AGENT_RUNNING', 'ANALYSING', 'APPROVED', 'PLAN_READY', "
    "'PR_OPEN', 'REVIEW_REQUIRED', 'WAITING_APPROVAL')"
)


def upgrade() -> None:
    op.create_index(
        'uq_foundry_runs_one_active_per_issue',
        'foundry_runs',
        ['linear_issue_id'],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATUS_PREDICATE),
        sqlite_where=sa.text(_ACTIVE_STATUS_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_foundry_runs_one_active_per_issue', table_name='foundry_runs'
    )
