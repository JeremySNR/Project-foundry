"""Change-freeze (maintenance) windows: time-scoped policy (issue #31).

A formal change-management process - an ITIL change freeze, a release blackout, a
"no autonomous changes on the weekend" rule - needs Foundry to *hold* autonomous
work during defined time windows and hand it to a human instead. This module is
the pure, offline core of that: a window model and an :func:`active_freeze`
predicate. The orchestrator consults it before an **autonomous re-dispatch**
(remediation retry) and, when a window is active, escalates the run to
``REVIEW_REQUIRED`` rather than firing the agent again.

It is **strictly additive** (invariant #1): a freeze can only ever hold an
autonomous action for a human, never release one. It is enforced in the
orchestrator lifecycle - like the forbidden-path block and the per-path approval
roles - so it touches neither ``policy/engine.py`` nor ``foundry.rego``; there is
no Python/Rego lock-step concern (invariant #2 does not apply). And it is pure:
no network and no clock of its own (the caller passes ``now``), so it tests
offline (invariant #3).

Two window shapes are supported:

* **recurring weekly** - ``weekdays`` (``mon``..``sun``) plus a ``start``/``end``
  ``HH:MM`` local time in an IANA ``tz`` (default ``UTC``). An ``end`` earlier
  than ``start`` wraps past midnight, anchored to its start weekday (so a
  ``fri`` ``17:00``->``09:00`` window is live Friday evening into Saturday
  morning).
* **absolute** - a ``starts_at``/``ends_at`` datetime range (e.g. a holiday code
  freeze). Naive datetimes are interpreted in the window's ``tz``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Canonical weekday order, lower-case three-letter abbreviations matching
# ``datetime.weekday()`` (Monday == 0).
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX: dict[str, int] = {name: i for i, name in enumerate(WEEKDAYS)}

_DAY_MINUTES = 24 * 60


@dataclass(frozen=True)
class ChangeFreezeWindow:
    """A single change-freeze window.

    Exactly one shape is populated: recurring (``weekdays`` + ``start`` +
    ``end``) or absolute (``starts_at`` + ``ends_at``). :func:`validate_window`
    enforces that; the dataclass itself stays a dumb, hashable record.
    """

    reason: str | None = None
    tz: str = "UTC"
    # Recurring weekly window.
    weekdays: tuple[str, ...] = ()
    start: str | None = None  # "HH:MM", local to ``tz``
    end: str | None = None  # "HH:MM", local to ``tz``
    # Absolute (calendar) window.
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @property
    def is_recurring(self) -> bool:
        return bool(self.weekdays)

    @property
    def is_absolute(self) -> bool:
        return self.starts_at is not None or self.ends_at is not None


# --------------------------------------------------------------------------- #
# Parsing (YAML mapping -> window)
# --------------------------------------------------------------------------- #
def window_from_mapping(data: Mapping[str, Any]) -> ChangeFreezeWindow:
    """Build a :class:`ChangeFreezeWindow` from a config mapping.

    Coercion only - structural typos (a non-ISO datetime) raise here; the
    semantic checks (exactly-one-shape, real weekdays, resolvable tz) live in
    :func:`validate_window` so they surface as a clear ``Settings`` load error.
    ``tz`` accepts the alias ``timezone`` for readability in YAML.
    """
    if not isinstance(data, Mapping):
        raise ValueError(
            f"each change_freeze_windows entry must be a mapping, got {data!r}"
        )
    tz = data.get("tz", data.get("timezone", "UTC"))
    weekdays = data.get("weekdays") or ()
    if isinstance(weekdays, str):
        weekdays = (weekdays,)
    weekdays = tuple(str(day).strip().lower() for day in weekdays)
    return ChangeFreezeWindow(
        reason=(str(data["reason"]) if data.get("reason") is not None else None),
        tz=str(tz),
        weekdays=weekdays,
        start=(str(data["start"]) if data.get("start") is not None else None),
        end=(str(data["end"]) if data.get("end") is not None else None),
        starts_at=_parse_dt(data.get("starts_at")),
        ends_at=_parse_dt(data.get("ends_at")),
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(
            f"change_freeze_windows datetime {value!r} is not ISO-8601: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_window(window: ChangeFreezeWindow) -> None:
    """Raise ``ValueError`` if ``window`` is not a well-formed freeze window."""
    if window.is_recurring and window.is_absolute:
        raise ValueError(
            "a change_freeze_windows entry is recurring (weekdays/start/end) OR "
            "absolute (starts_at/ends_at), not both"
        )
    # ``tz`` must resolve for either shape (recurring uses it for the local
    # clock; absolute uses it for naive datetimes).
    try:
        ZoneInfo(window.tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(
            f"change_freeze_windows tz {window.tz!r} is not a known IANA zone: {exc}"
        ) from exc

    if window.is_recurring:
        bad = [day for day in window.weekdays if day not in _WEEKDAY_INDEX]
        if bad:
            raise ValueError(
                f"change_freeze_windows lists unknown weekdays {bad}; valid days "
                f"are {list(WEEKDAYS)}"
            )
        if window.start is None or window.end is None:
            raise ValueError(
                "a recurring change_freeze_windows entry needs both 'start' and "
                "'end' (HH:MM)"
            )
        start = _parse_hhmm(window.start)
        end = _parse_hhmm(window.end)
        if start == end:
            raise ValueError(
                f"change_freeze_windows start and end are equal ({window.start}); "
                "an empty window freezes nothing - use 00:00/23:59 for a full day"
            )
    elif window.is_absolute:
        if window.starts_at is None or window.ends_at is None:
            raise ValueError(
                "an absolute change_freeze_windows entry needs both 'starts_at' "
                "and 'ends_at'"
            )
        if _as_aware(window.ends_at, window.tz) <= _as_aware(
            window.starts_at, window.tz
        ):
            raise ValueError(
                "change_freeze_windows 'ends_at' must be after 'starts_at'"
            )
    else:
        raise ValueError(
            "a change_freeze_windows entry must be recurring (weekdays/start/end) "
            "or absolute (starts_at/ends_at)"
        )


def validate_windows(windows: Sequence[ChangeFreezeWindow]) -> None:
    """Validate every window, with the entry index in the error for triage."""
    for index, window in enumerate(windows):
        try:
            validate_window(window)
        except ValueError as exc:
            raise ValueError(
                f"policy.change_freeze_windows[{index}] is invalid: {exc}"
            ) from exc


def _parse_hhmm(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"time {value!r} must be 'HH:MM'")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"time {value!r} must be 'HH:MM'") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"time {value!r} is out of range")
    return time(hour, minute)


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #
def active_freeze(
    windows: Sequence[ChangeFreezeWindow], now: datetime
) -> ChangeFreezeWindow | None:
    """Return the first window active at ``now``, or ``None`` if none is.

    ``now`` is treated as UTC if naive. Windows are tested in configured order,
    so the returned window is the first match - good enough for the
    "is anything frozen right now, and why" question the orchestrator asks.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for window in windows:
        if _window_active(window, now):
            return window
    return None


def _window_active(window: ChangeFreezeWindow, now: datetime) -> bool:
    if window.is_absolute:
        assert window.starts_at is not None and window.ends_at is not None
        start = _as_aware(window.starts_at, window.tz)
        end = _as_aware(window.ends_at, window.tz)
        return start <= now < end

    # Recurring: evaluate the local clock in the window's zone.
    zone = ZoneInfo(window.tz)
    local = now.astimezone(zone)
    start = _parse_hhmm(window.start or "00:00")
    duration = _duration_minutes(window.start or "00:00", window.end or "00:00")
    # A window may have begun on its start weekday and run past midnight, so
    # check both today's and yesterday's anchor (duration is <= 24h).
    for offset in (0, 1):
        anchor_date = (local - timedelta(days=offset)).date()
        if WEEKDAYS[anchor_date.weekday()] not in window.weekdays:
            continue
        anchor = datetime(
            anchor_date.year,
            anchor_date.month,
            anchor_date.day,
            start.hour,
            start.minute,
            tzinfo=zone,
        )
        if anchor <= local < anchor + timedelta(minutes=duration):
            return True
    return False


def _duration_minutes(start: str, end: str) -> int:
    """Minutes from ``start`` to ``end``, wrapping past midnight if end <= start."""
    s = _parse_hhmm(start)
    e = _parse_hhmm(end)
    delta = (e.hour * 60 + e.minute) - (s.hour * 60 + s.minute)
    return delta if delta > 0 else delta + _DAY_MINUTES


def _as_aware(value: datetime, tz: str) -> datetime:
    """Attach ``tz`` to a naive datetime; leave an aware one untouched."""
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(tz))
    return value


# --------------------------------------------------------------------------- #
# Presentation / comparison helpers (used by explain + check)
# --------------------------------------------------------------------------- #
def describe_window(window: ChangeFreezeWindow) -> str:
    """A short, human-readable one-liner for ``explain`` / the dashboard."""
    if window.is_absolute:
        body = (
            f"{_iso(window.starts_at)} -> {_iso(window.ends_at)} ({window.tz})"
        )
    else:
        days = "/".join(
            day for day in WEEKDAYS if day in window.weekdays
        ) or "(no days)"
        body = f"{days} {window.start}-{window.end} {window.tz}"
    if window.reason:
        return f"{body} - {window.reason}"
    return body


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "?"


def window_key(window: ChangeFreezeWindow) -> tuple[Any, ...]:
    """A canonical, hashable identity for a window, ignoring ``reason``.

    Used by ``compare_policy_strictness`` to ask whether a subject config's
    freeze windows are a superset of a baseline's: two windows that freeze the
    same time are equal regardless of their human-readable label.
    """
    if window.is_absolute:
        return (
            "absolute",
            window.tz,
            _as_aware(window.starts_at, window.tz) if window.starts_at else None,
            _as_aware(window.ends_at, window.tz) if window.ends_at else None,
        )
    return (
        "recurring",
        window.tz,
        tuple(day for day in WEEKDAYS if day in window.weekdays),
        window.start,
        window.end,
    )


__all__ = [
    "WEEKDAYS",
    "ChangeFreezeWindow",
    "window_from_mapping",
    "validate_window",
    "validate_windows",
    "active_freeze",
    "describe_window",
    "window_key",
]
