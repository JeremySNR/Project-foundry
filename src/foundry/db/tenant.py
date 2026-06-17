"""Tenant (org) context for row-level isolation (issue #156).

Foundry is multi-tenant at the row level: every tenant-scoped table carries an
``org_id`` (see ``db/models.py``) and the active org for the *current* unit of
work is held in a :class:`~contextvars.ContextVar`. The DB session machinery in
``db/base.py`` reads this contextvar to

* **stamp** ``org_id`` on every new tenant-scoped row at flush time, and
* **filter** every ORM ``SELECT`` to the current org (a ``with_loader_criteria``
  applied in a ``do_orm_execute`` listener),

so a unit of work scoped to one org can neither read nor write another org's
rows. The contextvar is *per execution context* (an ``asyncio`` task or a
threadpool worker each get their own copy), so a value set while handling one
request never leaks into another.

The default value is :data:`DEFAULT_ORG_ID`. A single-tenant deployment — the
historical behaviour and the offline test baseline — never sets the contextvar,
so every row is written under, and read back under, the one default org: the
filter ``org_id == 'default'`` matches every row and the behaviour is
byte-for-byte unchanged.

**Invariant #5:** the active org is derived only from the *authenticated
principal* (a verified OIDC claim — see ``api/app.py``) or left at the default,
never from a request payload. Nothing here reads request input.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

# The org every row falls under when no tenant is explicitly in scope. Existing
# rows are backfilled to this value by the migration, and a single-tenant
# deployment stays entirely within it.
DEFAULT_ORG_ID = "default"

_current_org: ContextVar[str] = ContextVar("foundry_current_org", default=DEFAULT_ORG_ID)


def current_org_id() -> str:
    """The org id for the current execution context (the default when unset)."""
    return _current_org.get()


def set_current_org(org_id: str) -> Token[str]:
    """Bind ``org_id`` to the current context; returns a reset token.

    A falsy/blank value falls back to :data:`DEFAULT_ORG_ID` so an empty claim
    can never silently widen access to an unscoped read.
    """
    return _current_org.set(org_id.strip() if org_id and org_id.strip() else DEFAULT_ORG_ID)


def reset_current_org(token: Token[str]) -> None:
    """Restore the org bound before the matching :func:`set_current_org`."""
    _current_org.reset(token)


@contextmanager
def tenant_context(org_id: str) -> Iterator[str]:
    """Run a block under ``org_id``, restoring the previous org on exit."""
    token = set_current_org(org_id)
    try:
        yield current_org_id()
    finally:
        reset_current_org(token)
