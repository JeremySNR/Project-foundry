"""SCIM 2.0 provisioning store and (de)serialisation (issue #157).

The enterprise identity item from #34: a SCIM 2.0 ``/Users`` and ``/Groups``
surface so an IdP (Okta, Entra ID, ...) can **provision and de-provision** the
users and groups Foundry recognises, instead of an operator hand-editing a
static approver list.

The hard rule, and the reason this is safe under invariant #5: **SCIM provisions
membership, never roles.** A provisioned user's approval authority is derived the
same way an OIDC token's is - an *active* user's group memberships are mapped
through the **committed** ``oidc_group_role_map`` (a group's ``displayName`` is
the lookup key) to approver roles. A SCIM request body can never name a role, so
a compromised provisioning credential can grant no authority the operator hasn't
already mapped to a group name in config. De-provisioning is the inverse:
deactivating a user (``active = false``, the standard SCIM de-provision signal),
removing it from a group, or deleting a group all *remove* derived authority -
strictly subtractive, so it can only ever make the gate stricter (invariant #1).

This module is the pure persistence + (de)serialisation layer. The FastAPI
routes and the bearer-token auth live in ``app.py``; the role resolution this
exposes (:meth:`ScimStore.resolve_identity`) is consulted by the approval path
there. The store relies on the tenant-context machinery in ``db/base.py`` to
stamp/filter ``org_id``, so it never touches org scoping itself.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select

from foundry.db.models import (
    FoundryScimGroup,
    FoundryScimGroupMember,
    FoundryScimUser,
)
from foundry.schemas.common import ApprovalRole

# SCIM 2.0 schema URNs (RFC 7643 / 7644).
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0.ListResponse"
PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0.PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0.Error"

_SCIM_TS = "%Y-%m-%dT%H:%M:%SZ"

# ``members[value eq "abc"]`` value-path filter (the Entra ID member-remove form).
_MEMBER_FILTER = re.compile(
    r'^members\[\s*value\s+eq\s+"(?P<id>[^"]+)"\s*\]$', re.IGNORECASE
)


class ScimError(Exception):
    """A SCIM-shaped error: an HTTP status plus the optional ``scimType``.

    Raised by the store and rendered by the route layer into the SCIM ``Error``
    response body (RFC 7644 §3.12). Never leaks a stack trace to the IdP.
    """

    def __init__(self, status: int, detail: str, *, scim_type: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(self.status)}
        if self.scim_type is not None:
            body["scimType"] = self.scim_type
        body["detail"] = self.detail
        return body


@dataclass(frozen=True)
class UserRecord:
    """A detached snapshot of a provisioned user (safe to use post-session)."""

    id: str
    user_name: str
    active: bool
    external_id: str | None
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    group_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemberRef:
    user_id: str
    user_name: str


@dataclass(frozen=True)
class GroupRecord:
    id: str
    display_name: str
    external_id: str | None
    created_at: datetime
    updated_at: datetime
    members: tuple[MemberRef, ...] = ()


@dataclass(frozen=True)
class Resolution:
    """The result of mapping an identity through the provisioned directory.

    ``provisioned`` is True only when the identity matches a SCIM user; callers
    use it to leave non-provisioned identities entirely unchanged (backward
    compatibility). ``active`` is False for a de-provisioned user, whose
    ``roles`` are always empty - the de-provisioning revocation.
    """

    provisioned: bool
    active: bool
    roles: frozenset[ApprovalRole] = field(default_factory=frozenset)


def _new_id() -> str:
    return uuid.uuid4().hex


def _coerce_active(value: Any) -> bool:
    """SCIM ``active`` tolerant of the bool / "true" / "false" string forms."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1", "yes"}:
            return True
        if value.strip().lower() in {"false", "0", "no", ""}:
            return False
    raise ScimError(400, f"invalid 'active' value: {value!r}", scim_type="invalidValue")


def _require_str(value: Any, attr: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScimError(
            400, f"'{attr}' must be a non-empty string", scim_type="invalidValue"
        )
    return value


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    raise ScimError(400, "attribute must be a string or null", scim_type="invalidValue")


def to_scim_user(rec: UserRecord, *, base_path: str = "/scim/v2") -> dict[str, Any]:
    """Render a :class:`UserRecord` as a SCIM 2.0 User resource."""
    body: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": rec.id,
        "userName": rec.user_name,
        "active": rec.active,
        "meta": {
            "resourceType": "User",
            "created": rec.created_at.strftime(_SCIM_TS),
            "lastModified": rec.updated_at.strftime(_SCIM_TS),
            "location": f"{base_path}/Users/{rec.id}",
        },
    }
    if rec.external_id is not None:
        body["externalId"] = rec.external_id
    if rec.display_name is not None:
        body["displayName"] = rec.display_name
    return body


def to_scim_group(rec: GroupRecord, *, base_path: str = "/scim/v2") -> dict[str, Any]:
    """Render a :class:`GroupRecord` as a SCIM 2.0 Group resource."""
    body: dict[str, Any] = {
        "schemas": [GROUP_SCHEMA],
        "id": rec.id,
        "displayName": rec.display_name,
        "members": [
            {
                "value": m.user_id,
                "display": m.user_name,
                "$ref": f"{base_path}/Users/{m.user_id}",
            }
            for m in rec.members
        ],
        "meta": {
            "resourceType": "Group",
            "created": rec.created_at.strftime(_SCIM_TS),
            "lastModified": rec.updated_at.strftime(_SCIM_TS),
            "location": f"{base_path}/Groups/{rec.id}",
        },
    }
    if rec.external_id is not None:
        body["externalId"] = rec.external_id
    return body


def list_response(
    resources: Sequence[dict[str, Any]], *, start_index: int = 1
) -> dict[str, Any]:
    """A SCIM 2.0 ListResponse envelope around already-rendered resources."""
    return {
        "schemas": [LIST_RESPONSE_SCHEMA],
        "totalResults": len(resources),
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": list(resources),
    }


def parse_username_filter(filter_expr: str | None) -> str | None:
    """Extract ``X`` from a ``userName eq "X"`` SCIM filter, else ``None``.

    IdPs probe for an existing user with exactly this filter before creating one;
    supporting it is what makes provisioning idempotent. Any other filter is
    treated as "no equality match requested" (returns ``None``), so the caller
    lists everything rather than mis-parsing an unsupported filter.
    """
    if not filter_expr:
        return None
    m = re.match(r'^\s*userName\s+eq\s+"(?P<v>[^"]*)"\s*$', filter_expr, re.IGNORECASE)
    return m.group("v") if m else None


def parse_displayname_filter(filter_expr: str | None) -> str | None:
    if not filter_expr:
        return None
    m = re.match(
        r'^\s*displayName\s+eq\s+"(?P<v>[^"]*)"\s*$', filter_expr, re.IGNORECASE
    )
    return m.group("v") if m else None


@dataclass
class ScimStore:
    """Persistence for SCIM users/groups, behind a SQLAlchemy session factory.

    Every method opens its own short unit of work; the tenant machinery in
    ``db/base.py`` stamps/filters ``org_id`` from the active context, so the store
    is org-agnostic. Records are returned detached (frozen dataclasses) so callers
    never touch a closed session.
    """

    session_factory: Any

    # ----- users -------------------------------------------------------------

    def _user_record(self, session, row: FoundryScimUser) -> UserRecord:
        group_ids = tuple(
            session.execute(
                select(FoundryScimGroupMember.group_id).where(
                    FoundryScimGroupMember.user_id == row.id
                )
            )
            .scalars()
            .all()
        )
        return UserRecord(
            id=row.id,
            user_name=row.user_name,
            active=row.active,
            external_id=row.external_id,
            display_name=row.display_name,
            created_at=row.created_at,
            updated_at=row.updated_at,
            group_ids=group_ids,
        )

    def create_user(
        self,
        *,
        user_name: str,
        external_id: str | None = None,
        display_name: str | None = None,
        active: Any = True,
    ) -> UserRecord:
        user_name = _require_str(user_name, "userName")
        active = _coerce_active(active)
        with self.session_factory() as session:
            existing = session.execute(
                select(FoundryScimUser).where(FoundryScimUser.user_name == user_name)
            ).scalar_one_or_none()
            if existing is not None:
                raise ScimError(
                    409,
                    f"a user with userName {user_name!r} already exists",
                    scim_type="uniqueness",
                )
            row = FoundryScimUser(
                id=_new_id(),
                user_name=user_name,
                external_id=external_id,
                display_name=display_name,
                active=active,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._user_record(session, row)

    def get_user(self, user_id: str) -> UserRecord | None:
        with self.session_factory() as session:
            row = session.get(FoundryScimUser, user_id)
            return self._user_record(session, row) if row is not None else None

    def find_user_by_user_name(self, user_name: str) -> UserRecord | None:
        with self.session_factory() as session:
            row = session.execute(
                select(FoundryScimUser).where(FoundryScimUser.user_name == user_name)
            ).scalar_one_or_none()
            return self._user_record(session, row) if row is not None else None

    def list_users(self, *, user_name: str | None = None) -> list[UserRecord]:
        with self.session_factory() as session:
            stmt = select(FoundryScimUser)
            if user_name is not None:
                stmt = stmt.where(FoundryScimUser.user_name == user_name)
            stmt = stmt.order_by(FoundryScimUser.created_at)
            rows = session.execute(stmt).scalars().all()
            return [self._user_record(session, r) for r in rows]

    def replace_user(
        self,
        user_id: str,
        *,
        user_name: str,
        external_id: str | None = None,
        display_name: str | None = None,
        active: Any = True,
    ) -> UserRecord:
        user_name = _require_str(user_name, "userName")
        active = _coerce_active(active)
        with self.session_factory() as session:
            row = session.get(FoundryScimUser, user_id)
            if row is None:
                raise ScimError(404, f"user {user_id!r} not found")
            clash = session.execute(
                select(FoundryScimUser).where(
                    FoundryScimUser.user_name == user_name,
                    FoundryScimUser.id != user_id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise ScimError(
                    409,
                    f"a user with userName {user_name!r} already exists",
                    scim_type="uniqueness",
                )
            row.user_name = user_name
            row.external_id = external_id
            row.display_name = display_name
            row.active = active
            session.commit()
            session.refresh(row)
            return self._user_record(session, row)

    def patch_user(self, user_id: str, operations: Any) -> UserRecord:
        with self.session_factory() as session:
            row = session.get(FoundryScimUser, user_id)
            if row is None:
                raise ScimError(404, f"user {user_id!r} not found")
            for op in _normalise_operations(operations):
                _apply_user_op(row, op)
            session.commit()
            session.refresh(row)
            return self._user_record(session, row)

    def delete_user(self, user_id: str) -> bool:
        """Hard-delete a user and its memberships. Returns False if absent."""
        with self.session_factory() as session:
            row = session.get(FoundryScimUser, user_id)
            if row is None:
                return False
            session.query(FoundryScimGroupMember).filter(
                FoundryScimGroupMember.user_id == user_id
            ).delete(synchronize_session=False)
            session.delete(row)
            session.commit()
            return True

    # ----- groups ------------------------------------------------------------

    def _group_record(self, session, row: FoundryScimGroup) -> GroupRecord:
        members = tuple(
            MemberRef(user_id=uid, user_name=uname)
            for uid, uname in session.execute(
                select(FoundryScimGroupMember.user_id, FoundryScimUser.user_name)
                .join(
                    FoundryScimUser,
                    FoundryScimUser.id == FoundryScimGroupMember.user_id,
                )
                .where(FoundryScimGroupMember.group_id == row.id)
                .order_by(FoundryScimUser.user_name)
            ).all()
        )
        return GroupRecord(
            id=row.id,
            display_name=row.display_name,
            external_id=row.external_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            members=members,
        )

    def _set_members(self, session, group_id: str, user_ids: Iterable[str]) -> None:
        wanted = list(dict.fromkeys(user_ids))  # de-dupe, keep order
        _validate_user_ids(session, wanted)
        session.query(FoundryScimGroupMember).filter(
            FoundryScimGroupMember.group_id == group_id
        ).delete(synchronize_session=False)
        for uid in wanted:
            session.add(
                FoundryScimGroupMember(id=_new_id(), group_id=group_id, user_id=uid)
            )

    def create_group(
        self,
        *,
        display_name: str,
        external_id: str | None = None,
        member_ids: Iterable[str] = (),
    ) -> GroupRecord:
        display_name = _require_str(display_name, "displayName")
        with self.session_factory() as session:
            existing = session.execute(
                select(FoundryScimGroup).where(
                    FoundryScimGroup.display_name == display_name
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ScimError(
                    409,
                    f"a group with displayName {display_name!r} already exists",
                    scim_type="uniqueness",
                )
            row = FoundryScimGroup(
                id=_new_id(), display_name=display_name, external_id=external_id
            )
            session.add(row)
            session.flush()
            self._set_members(session, row.id, member_ids)
            session.commit()
            session.refresh(row)
            return self._group_record(session, row)

    def get_group(self, group_id: str) -> GroupRecord | None:
        with self.session_factory() as session:
            row = session.get(FoundryScimGroup, group_id)
            return self._group_record(session, row) if row is not None else None

    def list_groups(self, *, display_name: str | None = None) -> list[GroupRecord]:
        with self.session_factory() as session:
            stmt = select(FoundryScimGroup)
            if display_name is not None:
                stmt = stmt.where(FoundryScimGroup.display_name == display_name)
            stmt = stmt.order_by(FoundryScimGroup.created_at)
            rows = session.execute(stmt).scalars().all()
            return [self._group_record(session, r) for r in rows]

    def replace_group(
        self,
        group_id: str,
        *,
        display_name: str,
        external_id: str | None = None,
        member_ids: Iterable[str] = (),
    ) -> GroupRecord:
        display_name = _require_str(display_name, "displayName")
        with self.session_factory() as session:
            row = session.get(FoundryScimGroup, group_id)
            if row is None:
                raise ScimError(404, f"group {group_id!r} not found")
            clash = session.execute(
                select(FoundryScimGroup).where(
                    FoundryScimGroup.display_name == display_name,
                    FoundryScimGroup.id != group_id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise ScimError(
                    409,
                    f"a group with displayName {display_name!r} already exists",
                    scim_type="uniqueness",
                )
            row.display_name = display_name
            row.external_id = external_id
            self._set_members(session, group_id, member_ids)
            session.commit()
            session.refresh(row)
            return self._group_record(session, row)

    def patch_group(self, group_id: str, operations: Any) -> GroupRecord:
        with self.session_factory() as session:
            row = session.get(FoundryScimGroup, group_id)
            if row is None:
                raise ScimError(404, f"group {group_id!r} not found")
            for op in _normalise_operations(operations):
                self._apply_group_op(session, row, op)
            row.updated_at = datetime.now(row.created_at.tzinfo)
            session.commit()
            session.refresh(row)
            return self._group_record(session, row)

    def delete_group(self, group_id: str) -> bool:
        with self.session_factory() as session:
            row = session.get(FoundryScimGroup, group_id)
            if row is None:
                return False
            session.query(FoundryScimGroupMember).filter(
                FoundryScimGroupMember.group_id == group_id
            ).delete(synchronize_session=False)
            session.delete(row)
            session.commit()
            return True

    def _apply_group_op(self, session, row: FoundryScimGroup, op: Mapping[str, Any]) -> None:
        kind = str(op.get("op", "")).lower()
        path = op.get("path")
        value = op.get("value")
        if path is None:
            # Path-less replace/add: a value object naming the attrs to set.
            if not isinstance(value, Mapping):
                raise ScimError(
                    400, "path-less patch requires an object value",
                    scim_type="invalidSyntax",
                )
            if "displayName" in value:
                row.display_name = _require_str(value["displayName"], "displayName")
            if "externalId" in value:
                row.external_id = _opt_str(value["externalId"])
            if "members" in value:
                self._set_members(
                    session, row.id, member_ids_from_value(value["members"])
                )
            return
        path = str(path)
        member_filter = _MEMBER_FILTER.match(path)
        if member_filter is not None:
            # ``members[value eq "id"]`` - only ``remove`` is meaningful.
            if kind != "remove":
                raise ScimError(
                    400, f"unsupported op {kind!r} on a member-filter path",
                    scim_type="invalidSyntax",
                )
            self._remove_members(session, row.id, [member_filter.group("id")])
            return
        if path.lower() == "displayname":
            if kind == "remove":
                raise ScimError(
                    400, "displayName is required and cannot be removed",
                    scim_type="invalidValue",
                )
            row.display_name = _require_str(value, "displayName")
            return
        if path.lower() == "externalid":
            row.external_id = None if kind == "remove" else _opt_str(value)
            return
        if path.lower() == "members":
            ids = member_ids_from_value(value)
            if kind in {"add"}:
                self._add_members(session, row.id, ids)
            elif kind == "replace":
                self._set_members(session, row.id, ids)
            elif kind == "remove":
                # No value => remove all members; else remove the named ones.
                if value is None:
                    session.query(FoundryScimGroupMember).filter(
                        FoundryScimGroupMember.group_id == row.id
                    ).delete(synchronize_session=False)
                else:
                    self._remove_members(session, row.id, ids)
            else:
                raise ScimError(
                    400, f"unsupported op {kind!r} on members", scim_type="invalidSyntax"
                )
            return
        raise ScimError(400, f"unsupported path {path!r}", scim_type="invalidPath")

    def _add_members(self, session, group_id: str, user_ids: Sequence[str]) -> None:
        _validate_user_ids(session, user_ids)
        present = set(
            session.execute(
                select(FoundryScimGroupMember.user_id).where(
                    FoundryScimGroupMember.group_id == group_id
                )
            )
            .scalars()
            .all()
        )
        for uid in dict.fromkeys(user_ids):
            if uid not in present:
                session.add(
                    FoundryScimGroupMember(id=_new_id(), group_id=group_id, user_id=uid)
                )

    def _remove_members(self, session, group_id: str, user_ids: Sequence[str]) -> None:
        if not user_ids:
            return
        session.query(FoundryScimGroupMember).filter(
            FoundryScimGroupMember.group_id == group_id,
            FoundryScimGroupMember.user_id.in_(list(user_ids)),
        ).delete(synchronize_session=False)

    # ----- role resolution ---------------------------------------------------

    def resolve_identity(
        self,
        identity: str,
        group_role_map: Mapping[str, Iterable[ApprovalRole]],
    ) -> Resolution:
        """Map an approver identity through the provisioned directory.

        ``identity`` is the verified subject (an OIDC ``email``/``sub``). When it
        matches a provisioned ``userName`` the result carries the user's
        live-membership-derived roles (a group's ``displayName`` looked up in the
        **committed** ``group_role_map``); a de-provisioned (inactive) user
        resolves to no roles. A non-provisioned identity returns
        ``provisioned=False`` so the caller leaves it untouched.
        """
        with self.session_factory() as session:
            user = session.execute(
                select(FoundryScimUser).where(FoundryScimUser.user_name == identity)
            ).scalar_one_or_none()
            if user is None:
                return Resolution(provisioned=False, active=False)
            if not user.active:
                return Resolution(provisioned=True, active=False)
            display_names = (
                session.execute(
                    select(FoundryScimGroup.display_name)
                    .join(
                        FoundryScimGroupMember,
                        FoundryScimGroupMember.group_id == FoundryScimGroup.id,
                    )
                    .where(FoundryScimGroupMember.user_id == user.id)
                )
                .scalars()
                .all()
            )
            roles: set[ApprovalRole] = set()
            for name in display_names:
                roles |= set(group_role_map.get(name, ()))
            return Resolution(
                provisioned=True, active=True, roles=frozenset(roles)
            )


# ----- patch helpers (module-level: pure, easy to unit-test) -----------------


def _normalise_operations(operations: Any) -> list[Mapping[str, Any]]:
    if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
        raise ScimError(
            400, "PATCH 'Operations' must be a list", scim_type="invalidSyntax"
        )
    ops: list[Mapping[str, Any]] = []
    for op in operations:
        if not isinstance(op, Mapping) or "op" not in op:
            raise ScimError(
                400, "each PATCH operation needs an 'op'", scim_type="invalidSyntax"
            )
        ops.append(op)
    return ops


def _apply_user_op(row: FoundryScimUser, op: Mapping[str, Any]) -> None:
    kind = str(op.get("op", "")).lower()
    path = op.get("path")
    value = op.get("value")
    if path is None:
        if not isinstance(value, Mapping):
            raise ScimError(
                400, "path-less patch requires an object value",
                scim_type="invalidSyntax",
            )
        _apply_user_attr_map(row, value)
        return
    attr = str(path).lower()
    if attr == "active":
        row.active = _coerce_active(value)
    elif attr == "displayname":
        row.display_name = None if kind == "remove" else _opt_str(value)
    elif attr == "externalid":
        row.external_id = None if kind == "remove" else _opt_str(value)
    elif attr == "username":
        if kind == "remove":
            raise ScimError(
                400, "userName is required and cannot be removed",
                scim_type="invalidValue",
            )
        row.user_name = _require_str(value, "userName")
    else:
        raise ScimError(400, f"unsupported path {path!r}", scim_type="invalidPath")


def _apply_user_attr_map(row: FoundryScimUser, value: Mapping[str, Any]) -> None:
    for key, val in value.items():
        low = key.lower()
        if low == "active":
            row.active = _coerce_active(val)
        elif low == "displayname":
            row.display_name = _opt_str(val)
        elif low == "externalid":
            row.external_id = _opt_str(val)
        elif low == "username":
            row.user_name = _require_str(val, "userName")
        # Unknown attributes are ignored (SCIM clients send extra core attrs).


def member_ids_from_value(value: Any) -> list[str]:
    """Pull the user ids out of a SCIM ``members`` value list (``[{value: id}]``)."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ScimError(
            400, "'members' value must be a list of member objects",
            scim_type="invalidSyntax",
        )
    ids: list[str] = []
    for member in value:
        if isinstance(member, Mapping):
            uid = member.get("value")
        else:
            uid = member
        if not isinstance(uid, str) or not uid:
            raise ScimError(
                400, "each member needs a string 'value' (user id)",
                scim_type="invalidValue",
            )
        ids.append(uid)
    return ids


def _validate_user_ids(session, user_ids: Sequence[str]) -> None:
    if not user_ids:
        return
    found = set(
        session.execute(
            select(FoundryScimUser.id).where(FoundryScimUser.id.in_(list(user_ids)))
        )
        .scalars()
        .all()
    )
    missing = [uid for uid in user_ids if uid not in found]
    if missing:
        raise ScimError(
            400,
            f"member user(s) not found: {', '.join(sorted(set(missing)))}",
            scim_type="invalidValue",
        )
