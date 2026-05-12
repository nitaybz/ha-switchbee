"""Pure-Python unit tests for the coordinator's LOGIN-timeout watchdog.

The watchdog is the Python expression of the cron-driven `kill homebridge if
LOGIN never returns` workaround we put on Moshe's machine. It does NOT need
Home Assistant to test: it is a small state machine that records the
timestamps of LOGIN timeouts in a rolling window and reports `tripped()`
once N events fall inside that window.

These tests are intentionally HA-free so we can run them under a plain
Python 3.12 venv even if PHCC ever stops working on 3.12.
"""

from __future__ import annotations

import pytest

from custom_components.ha_switchbee.coordinator import LoginTimeoutWatchdog


class TestLoginTimeoutWatchdog:
    """Cover the rolling-window counter behavior in isolation."""

    def test_starts_untripped(self) -> None:
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        assert wd.tripped() is False
        assert wd.count() == 0

    def test_does_not_trip_below_threshold(self) -> None:
        """Two timeouts inside the window are not enough."""
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        wd.record_timeout(now=1000.0)
        wd.record_timeout(now=1001.0)
        assert wd.count() == 2
        assert wd.tripped() is False

    def test_trips_at_threshold_inside_window(self) -> None:
        """The N-th timeout inside the window trips the watchdog."""
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        wd.record_timeout(now=1000.0)
        wd.record_timeout(now=1100.0)
        wd.record_timeout(now=1200.0)
        assert wd.count() == 3
        assert wd.tripped() is True

    def test_old_events_are_evicted(self) -> None:
        """Timeouts outside the rolling window do not count.

        Window is 300s. After recording at 1000, 1250, 1350:
        the cutoff is 1350 - 300 = 1050. Events <= 1050 evict (1000),
        leaving 1250 and 1350. Count = 2, below threshold of 3.
        """
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        wd.record_timeout(now=1000.0)
        wd.record_timeout(now=1250.0)
        wd.record_timeout(now=1350.0)
        assert wd.count() == 2
        assert wd.tripped() is False

    def test_reset_clears_state(self) -> None:
        """Calling reset() drops all recorded timeouts."""
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        for ts in (1000.0, 1010.0, 1020.0):
            wd.record_timeout(now=ts)
        assert wd.tripped() is True
        wd.reset()
        assert wd.count() == 0
        assert wd.tripped() is False

    def test_trips_again_after_reset_and_three_more(self) -> None:
        """After reset, the watchdog can trip again on three fresh timeouts."""
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        for ts in (1000.0, 1010.0, 1020.0):
            wd.record_timeout(now=ts)
        wd.reset()
        for ts in (2000.0, 2010.0, 2020.0):
            wd.record_timeout(now=ts)
        assert wd.tripped() is True

    def test_invalid_threshold_raises(self) -> None:
        with pytest.raises(ValueError):
            LoginTimeoutWatchdog(threshold=0, window_seconds=300.0)
        with pytest.raises(ValueError):
            LoginTimeoutWatchdog(threshold=-1, window_seconds=300.0)

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            LoginTimeoutWatchdog(threshold=3, window_seconds=0.0)
        with pytest.raises(ValueError):
            LoginTimeoutWatchdog(threshold=3, window_seconds=-10.0)

    def test_threshold_one_trips_on_first_event(self) -> None:
        """Threshold=1 means trip immediately on the first timeout."""
        wd = LoginTimeoutWatchdog(threshold=1, window_seconds=60.0)
        assert wd.tripped() is False
        wd.record_timeout(now=500.0)
        assert wd.tripped() is True

    def test_events_at_exact_window_boundary_are_evicted(self) -> None:
        """An event exactly `window_seconds` old is no longer counted."""
        wd = LoginTimeoutWatchdog(threshold=3, window_seconds=300.0)
        wd.record_timeout(now=1000.0)
        wd.record_timeout(now=1100.0)
        wd.record_timeout(now=1300.0)
        # 1300.0 is exactly 300s after 1000.0 -> evict 1000.0.
        assert wd.count() == 2
        assert wd.tripped() is False
