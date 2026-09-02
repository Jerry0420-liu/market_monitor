"""CR-004 TradingClock-aware minute-tail behavior around lunch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from market_monitor_analysis.metric_runner import (
    _Bar,
    _complete_current_tail,
    _same_clock_amount,
)
from market_monitor_data.clock import TradingClock
from market_monitor_data.tdx.historical import _current_window, _window_timestamps
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue


@pytest.mark.parametrize(
    ("as_of", "expected_tail_size"),
    (
        (datetime(2026, 8, 24, 3, 30, tzinfo=UTC), 15),
        (datetime(2026, 8, 24, 5, 0, tzinfo=UTC), 0),
        (datetime(2026, 8, 24, 5, 1, tzinfo=UTC), 1),
        (datetime(2026, 8, 24, 5, 5, tzinfo=UTC), 5),
        (datetime(2026, 8, 24, 5, 10, tzinfo=UTC), 10),
        (datetime(2026, 8, 24, 5, 15, tzinfo=UTC), 15),
    ),
)
def test_current_tail_uses_legal_session_minutes_without_crossing_lunch(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    as_of: datetime,
    expected_tail_size: int,
) -> None:
    """Break if stale AM bars satisfy a PM tail or lunch makes PM globally stale."""
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    clock.import_day(
        "SSE",
        "2026-08-24",
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", "2026-08-24T01:30:00Z", "2026-08-24T03:30:00Z"),
            ("CONTINUOUS_PM", "2026-08-24T05:00:00Z", "2026-08-24T07:00:00Z"),
        ],
    )
    am_start = datetime(2026, 8, 24, 3, 16, tzinfo=UTC)
    pm_start = datetime(2026, 8, 24, 5, 1, tzinfo=UTC)
    bars = tuple(
        _Bar("member", "1m", instant, Decimal("10"), Decimal("100"), "epoch")
        for instant in (
            *(am_start + timedelta(minutes=index) for index in range(15)),
            *(pm_start + timedelta(minutes=index) for index in range(15)),
        )
    )

    tail = _complete_current_tail(bars, as_of, clock, "SSE")

    assert len(tail or ()) == expected_tail_size


def test_complete_bar_windows_use_right_edge_labels(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    clock.import_day(
        "SSE",
        "2026-08-25",
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", "2026-08-25T01:30:00Z", "2026-08-25T03:30:00Z"),
            ("CONTINUOUS_PM", "2026-08-25T05:00:00Z", "2026-08-25T07:00:00Z"),
        ],
    )
    target = datetime(2026, 8, 25, 1, 45, tzinfo=UTC)
    expected_15 = tuple(
        datetime(2026, 8, 25, 1, 31, tzinfo=UTC) + timedelta(minutes=index) for index in range(15)
    )
    bars = tuple(
        _Bar("member", "1m", instant, Decimal("10"), Decimal("1"), "epoch")
        for instant in expected_15
    )

    tail = _complete_current_tail(bars, target, clock, "SSE")

    assert _current_window(clock, "SSE", target) == expected_15
    assert _current_window(clock, "SSE", target - timedelta(minutes=1)) == ()
    assert tuple(item.source_time for item in tail or ()) == expected_15
    assert tuple(item.source_time for item in (tail or ())[-5:]) == expected_15[-5:]
    assert tuple(item.source_time for item in (tail or ())[-10:]) == expected_15[-10:]
    assert datetime(2026, 8, 25, 1, 30, tzinfo=UTC) not in expected_15

    prior_expected = {
        datetime(2026, 8, 24, 1, 41, tzinfo=UTC) + timedelta(minutes=index) for index in range(5)
    }
    prior_bars = tuple(
        _Bar("member", "1m", instant, Decimal("10"), Decimal("1"), "epoch")
        for instant in prior_expected
    )
    assert _window_timestamps("2026-08-24", 9, 45) == prior_expected
    assert _same_clock_amount(prior_bars, "2026-08-24", target) == Decimal("5")


def test_historical_window_respects_right_edge_session_boundaries(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    clock.import_day(
        "SSE",
        "2026-08-25",
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", "2026-08-25T01:30:00Z", "2026-08-25T03:30:00Z"),
            ("CONTINUOUS_PM", "2026-08-25T05:00:00Z", "2026-08-25T07:00:00Z"),
        ],
    )

    assert _current_window(clock, "SSE", datetime(2026, 8, 25, 3, 30, tzinfo=UTC)) == tuple(
        datetime(2026, 8, 25, 3, 16, tzinfo=UTC) + timedelta(minutes=index) for index in range(15)
    )
    assert _current_window(clock, "SSE", datetime(2026, 8, 25, 5, 1, tzinfo=UTC)) == ()
    assert _current_window(clock, "SSE", datetime(2026, 8, 25, 5, 15, tzinfo=UTC)) == tuple(
        datetime(2026, 8, 25, 5, 1, tzinfo=UTC) + timedelta(minutes=index) for index in range(15)
    )


@pytest.mark.parametrize(
    ("at", "expected"),
    (
        (datetime(2026, 8, 25, 1, 30, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 1, 30, 59, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 1, 31, tzinfo=UTC), datetime(2026, 8, 25, 1, 31, tzinfo=UTC)),
        (datetime(2026, 8, 25, 3, 29, 59, tzinfo=UTC), datetime(2026, 8, 25, 3, 29, tzinfo=UTC)),
        (datetime(2026, 8, 25, 3, 30, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 5, 0, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 5, 0, 59, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 5, 1, tzinfo=UTC), datetime(2026, 8, 25, 5, 1, tzinfo=UTC)),
        (datetime(2026, 8, 25, 7, 0, tzinfo=UTC), None),
    ),
)
def test_latest_completed_continuous_minute_never_crosses_a_session_boundary(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    at: datetime,
    expected: datetime | None,
) -> None:
    """A live 1m cohort must be a completed minute of the current legal session."""
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    for exchange in ("SSE", "SZSE"):
        clock.import_day(
            exchange,
            "2026-08-25",
            "Asia/Shanghai",
            [
                ("CONTINUOUS_AM", "2026-08-25T01:30:00Z", "2026-08-25T03:30:00Z"),
                ("CONTINUOUS_PM", "2026-08-25T05:00:00Z", "2026-08-25T07:00:00Z"),
            ],
        )

    assert clock.latest_completed_continuous_minute("SSE", at) == expected


@pytest.mark.parametrize(
    ("at", "expected"),
    (
        (datetime(2026, 8, 25, 1, 29, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 1, 30, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 1, 30, 59, tzinfo=UTC), None),
        (datetime(2026, 8, 25, 1, 31, tzinfo=UTC), datetime(2026, 8, 25, 1, 31, tzinfo=UTC)),
        (datetime(2026, 8, 25, 3, 30, tzinfo=UTC), datetime(2026, 8, 25, 3, 30, tzinfo=UTC)),
        (datetime(2026, 8, 25, 4, 0, tzinfo=UTC), datetime(2026, 8, 25, 3, 30, tzinfo=UTC)),
        (datetime(2026, 8, 25, 5, 0, tzinfo=UTC), datetime(2026, 8, 25, 3, 30, tzinfo=UTC)),
        (datetime(2026, 8, 25, 5, 0, 59, tzinfo=UTC), datetime(2026, 8, 25, 3, 30, tzinfo=UTC)),
        (datetime(2026, 8, 25, 5, 1, tzinfo=UTC), datetime(2026, 8, 25, 5, 1, tzinfo=UTC)),
        (datetime(2026, 8, 25, 7, 0, tzinfo=UTC), datetime(2026, 8, 25, 7, 0, tzinfo=UTC)),
        (datetime(2026, 8, 25, 8, 0, tzinfo=UTC), datetime(2026, 8, 25, 7, 0, tzinfo=UTC)),
    ),
)
def test_latest_completed_legal_minute_is_stable_during_breaks_and_after_close(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    at: datetime,
    expected: datetime | None,
) -> None:
    """Historical coverage uses the legal clock, never the latest stored bar."""
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    clock.import_day(
        "SSE",
        "2026-08-25",
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", "2026-08-25T01:30:00Z", "2026-08-25T03:30:00Z"),
            ("CONTINUOUS_PM", "2026-08-25T05:00:00Z", "2026-08-25T07:00:00Z"),
        ],
    )

    assert clock.latest_completed_legal_minute("SSE", at) == expected
