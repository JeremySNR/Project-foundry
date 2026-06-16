"""Change-freeze / maintenance windows (issue #31, the "time windows" policy).

These tests are pure and offline (invariant #3): the predicate takes ``now`` as
an argument, so no wall clock is involved. They cover the window model, the
active-freeze predicate (recurring + absolute + wrap-past-midnight + tz), config
parsing/validation, and the explain/check surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foundry.config import Settings
from foundry.policy.freeze import (
    ChangeFreezeWindow,
    active_freeze,
    describe_window,
    validate_window,
    validate_windows,
    window_from_mapping,
    window_key,
)
from foundry.policy.library import (
    compare_policy_strictness,
    effective_policy_summary,
    load_preset_settings,
)

UTC = timezone.utc


def _weekend() -> ChangeFreezeWindow:
    return ChangeFreezeWindow(
        reason="weekend", weekdays=("sat", "sun"), start="00:00", end="23:59", tz="UTC"
    )


# --------------------------------------------------------------------------- #
# The predicate: recurring windows
# --------------------------------------------------------------------------- #
def test_recurring_window_active_on_listed_weekday() -> None:
    # 2026-06-20 is a Saturday.
    assert active_freeze([_weekend()], datetime(2026, 6, 20, 12, 0, tzinfo=UTC))


def test_recurring_window_inactive_off_listed_weekday() -> None:
    # 2026-06-17 is a Wednesday.
    assert active_freeze([_weekend()], datetime(2026, 6, 17, 12, 0, tzinfo=UTC)) is None


def test_recurring_window_respects_start_and_end_times() -> None:
    window = ChangeFreezeWindow(
        weekdays=("mon",), start="09:00", end="17:00", tz="UTC"
    )
    monday = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)  # Monday
    assert active_freeze([window], monday.replace(hour=8)) is None  # before start
    assert active_freeze([window], monday.replace(hour=12)) is not None  # inside
    assert active_freeze([window], monday.replace(hour=17)) is None  # end exclusive


def test_recurring_window_wraps_past_midnight() -> None:
    # A Friday-evening freeze that runs into Saturday morning.
    window = ChangeFreezeWindow(
        weekdays=("fri",), start="22:00", end="02:00", tz="UTC"
    )
    friday = datetime(2026, 6, 19, 0, 0, tzinfo=UTC)  # Friday
    assert active_freeze([window], friday.replace(hour=23)) is not None  # Fri late
    saturday = datetime(2026, 6, 20, 0, 0, tzinfo=UTC)
    assert active_freeze([window], saturday.replace(hour=1)) is not None  # Sat 01:00
    assert active_freeze([window], saturday.replace(hour=3)) is None  # past the wrap


def test_recurring_window_uses_its_timezone() -> None:
    # Monday business hours in New York; "now" is given in UTC.
    window = ChangeFreezeWindow(
        weekdays=("mon",), start="09:00", end="17:00", tz="America/New_York"
    )
    # 2026-06-15 13:00 New York (EDT, UTC-4) == 17:00 UTC, still a NY Monday.
    assert active_freeze([window], datetime(2026, 6, 15, 17, 0, tzinfo=UTC)) is not None
    # 07:00 NY (11:00 UTC) is before the window opens.
    assert active_freeze([window], datetime(2026, 6, 15, 11, 0, tzinfo=UTC)) is None


def test_naive_now_is_treated_as_utc() -> None:
    assert active_freeze([_weekend()], datetime(2026, 6, 20, 12, 0)) is not None


# --------------------------------------------------------------------------- #
# The predicate: absolute windows + ordering
# --------------------------------------------------------------------------- #
def test_absolute_window_active_inside_range() -> None:
    window = ChangeFreezeWindow(
        reason="holiday",
        starts_at=datetime(2026, 12, 20, 0, 0),
        ends_at=datetime(2027, 1, 2, 0, 0),
        tz="UTC",
    )
    assert active_freeze([window], datetime(2026, 12, 25, 12, 0, tzinfo=UTC))
    assert active_freeze([window], datetime(2026, 12, 19, 12, 0, tzinfo=UTC)) is None
    # End is exclusive.
    assert active_freeze([window], datetime(2027, 1, 2, 0, 0, tzinfo=UTC)) is None


def test_active_freeze_returns_first_matching_window() -> None:
    first = ChangeFreezeWindow(reason="weekend", weekdays=("sat",), start="00:00", end="23:59")
    second = ChangeFreezeWindow(reason="all-saturday-too", weekdays=("sat",), start="10:00", end="14:00")
    hit = active_freeze([first, second], datetime(2026, 6, 20, 12, 0, tzinfo=UTC))
    assert hit is first


def test_no_windows_is_never_frozen() -> None:
    assert active_freeze([], datetime(2026, 6, 20, 12, 0, tzinfo=UTC)) is None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_window_from_mapping_recurring() -> None:
    window = window_from_mapping(
        {"weekdays": ["Sat", "SUN"], "start": "00:00", "end": "23:59", "tz": "UTC"}
    )
    assert window.weekdays == ("sat", "sun")  # normalised to lower-case
    assert window.is_recurring and not window.is_absolute


def test_window_from_mapping_absolute_with_iso_strings_and_tz_alias() -> None:
    window = window_from_mapping(
        {
            "starts_at": "2026-12-20T00:00:00",
            "ends_at": "2027-01-02T00:00:00",
            "timezone": "America/New_York",  # alias for tz
        }
    )
    assert window.is_absolute
    assert window.tz == "America/New_York"
    assert window.starts_at == datetime(2026, 12, 20, 0, 0)


def test_window_from_mapping_single_weekday_string() -> None:
    assert window_from_mapping({"weekdays": "fri", "start": "1:00", "end": "2:00"}).weekdays == ("fri",)


def test_window_from_mapping_rejects_bad_datetime() -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        window_from_mapping({"starts_at": "not-a-date", "ends_at": "also-not"})


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_validate_rejects_unknown_weekday() -> None:
    with pytest.raises(ValueError, match="unknown weekdays"):
        validate_window(ChangeFreezeWindow(weekdays=("funday",), start="00:00", end="23:59"))


def test_validate_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="IANA zone"):
        validate_window(ChangeFreezeWindow(weekdays=("mon",), start="00:00", end="23:59", tz="Mars/Olympus"))


def test_validate_rejects_recurring_without_times() -> None:
    with pytest.raises(ValueError, match="both 'start' and 'end'"):
        validate_window(ChangeFreezeWindow(weekdays=("mon",), start="09:00"))


def test_validate_rejects_both_shapes() -> None:
    with pytest.raises(ValueError, match="not both"):
        validate_window(
            ChangeFreezeWindow(
                weekdays=("mon",), start="00:00", end="23:59",
                starts_at=datetime(2026, 1, 1), ends_at=datetime(2026, 1, 2),
            )
        )


def test_validate_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="recurring .* or absolute"):
        validate_window(ChangeFreezeWindow())


def test_validate_rejects_equal_start_and_end() -> None:
    with pytest.raises(ValueError, match="equal"):
        validate_window(ChangeFreezeWindow(weekdays=("mon",), start="09:00", end="09:00"))


def test_validate_rejects_absolute_end_before_start() -> None:
    with pytest.raises(ValueError, match="after 'starts_at'"):
        validate_window(
            ChangeFreezeWindow(
                starts_at=datetime(2027, 1, 2), ends_at=datetime(2026, 12, 20)
            )
        )


def test_validate_windows_reports_offending_index() -> None:
    windows = [_weekend(), ChangeFreezeWindow(weekdays=("nope",), start="00:00", end="23:59")]
    with pytest.raises(ValueError, match=r"change_freeze_windows\[1\]"):
        validate_windows(windows)


# --------------------------------------------------------------------------- #
# Config integration
# --------------------------------------------------------------------------- #
_FREEZE_YAML = """
policy:
  change_freeze_windows:
    - reason: "Weekend release blackout"
      weekdays: ["sat", "sun"]
      start: "00:00"
      end: "23:59"
      tz: "UTC"
    - reason: "Year-end freeze"
      starts_at: "2026-12-20T00:00:00"
      ends_at: "2027-01-02T00:00:00"
      tz: "America/New_York"
"""


def test_settings_loads_and_validates_freeze_windows(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(_FREEZE_YAML)
    settings = Settings.load(path, env={})
    assert len(settings.change_freeze_windows) == 2
    assert settings.change_freeze_windows[0].weekdays == ("sat", "sun")
    assert settings.change_freeze_windows[1].is_absolute


def test_settings_rejects_malformed_freeze_window(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(
        "policy:\n"
        "  change_freeze_windows:\n"
        "    - weekdays: [\"someday\"]\n"
        "      start: \"00:00\"\n"
        "      end: \"23:59\"\n"
    )
    with pytest.raises(ValueError, match="change_freeze_windows"):
        Settings.load(path, env={})


# --------------------------------------------------------------------------- #
# explain + check surfaces
# --------------------------------------------------------------------------- #
def test_effective_summary_surfaces_freeze_windows() -> None:
    cm = effective_policy_summary(load_preset_settings("change-management"))
    assert any("sat" in line for line in cm["change_freeze_windows"])
    # A preset that sets none reports an empty list, not a missing key.
    assert effective_policy_summary(load_preset_settings("baseline"))[
        "change_freeze_windows"
    ] == []


def test_compare_flags_missing_freeze_window() -> None:
    baseline = Settings(change_freeze_windows=(_weekend(),))
    subject = Settings()  # no freeze at all -> weaker
    comparison = compare_policy_strictness(subject, baseline)
    weak = {f.knob for f in comparison.weaknesses}
    assert "change_freeze_windows" in weak


def test_compare_passes_when_subject_covers_baseline_freeze() -> None:
    baseline = Settings(change_freeze_windows=(_weekend(),))
    # Same window, a different (re-worded) reason: identity is the time, not the label.
    relabelled = ChangeFreezeWindow(
        reason="different words", weekdays=("sat", "sun"), start="00:00", end="23:59", tz="UTC"
    )
    subject = Settings(change_freeze_windows=(relabelled,))
    comparison = compare_policy_strictness(subject, baseline)
    finding = next(f for f in comparison.findings if f.knob == "change_freeze_windows")
    assert finding.ok


def test_compare_freeze_ok_when_baseline_requires_none() -> None:
    finding = next(
        f
        for f in compare_policy_strictness(
            Settings(change_freeze_windows=(_weekend(),)), Settings()
        ).findings
        if f.knob == "change_freeze_windows"
    )
    assert finding.ok


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #
def test_describe_window_recurring_and_absolute() -> None:
    assert "sat/sun" in describe_window(_weekend())
    absolute = ChangeFreezeWindow(
        reason="holiday", starts_at=datetime(2026, 12, 20), ends_at=datetime(2027, 1, 2)
    )
    assert "->" in describe_window(absolute) and "holiday" in describe_window(absolute)


def test_window_key_ignores_reason() -> None:
    a = ChangeFreezeWindow(reason="a", weekdays=("sat",), start="00:00", end="23:59")
    b = ChangeFreezeWindow(reason="b", weekdays=("sat",), start="00:00", end="23:59")
    assert window_key(a) == window_key(b)
