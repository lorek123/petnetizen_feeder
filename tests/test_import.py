"""Minimal tests so CI passes; extend with real BLE tests when needed."""

import pytest

from petnetizen_feeder import FeederDevice, FeedSchedule, Weekday


def test_import():
    """Package imports and exposes public API."""
    assert FeederDevice is not None
    assert FeedSchedule is not None
    assert Weekday is not None


def test_weekday_constants():
    """Weekday constants are defined."""
    assert Weekday.ALL_DAYS == ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    assert "mon" in Weekday.WEEKDAYS
    assert "sat" in Weekday.WEEKEND


def test_feed_schedule_to_bytes():
    """FeedSchedule serializes to protocol bytes."""
    s = FeedSchedule(weekdays=Weekday.ALL_DAYS, time="08:00", portions=1, enabled=True)
    raw = s.to_bytes()
    assert len(raw) == 5
    assert raw[0] == 0x7F  # all days bitmask
    assert raw[1] == 8
    assert raw[2] == 0
    assert raw[3] == 1
    assert raw[4] == 1
