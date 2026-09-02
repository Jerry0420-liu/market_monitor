import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from market_monitor_data.clock import TradingClock
from market_monitor_data.providers import (
    DisabledLiveProvider,
    FixtureReplayProvider,
    ProviderDisabledError,
)


def test_clock_uses_imported_sessions_and_flags_bad_source_time(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m2_runtime
    clock = TradingClock(runtime, writer)
    clock.import_day(
        "SSE",
        "2026-08-04",
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", "2026-08-04T01:30:00Z", "2026-08-04T03:30:00Z"),
            ("CONTINUOUS_PM", "2026-08-04T05:00:00Z", "2026-08-04T07:00:00Z"),
        ],
    )
    clock.import_day("SSE", "2026-08-03", "Asia/Shanghai", [])
    assert clock.phase_at("SSE", datetime(2026, 8, 4, 2, tzinfo=UTC)) == "CONTINUOUS_AM"
    assert clock.phase_at("SSE", datetime(2026, 8, 4, 4, tzinfo=UTC)) == "BREAK"
    assert clock.phase_at("SSE", datetime(2026, 8, 3, 2, tzinfo=UTC)) == "NON_TRADING_DAY"
    assert clock.phase_at("SSE", datetime(2026, 8, 2, 2, tzinfo=UTC)) == "CALENDAR_COVERAGE_MISSING"
    assert clock.validate_source_time("not-a-time") is None


def test_clock_calendar_import_is_idempotent_but_rejects_conflicting_history(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    """Owner-supplied calendar reloads cannot rewrite an existing trading day."""
    runtime, writer, _ = m2_runtime
    clock = TradingClock(runtime, writer)
    sessions = [
        ("CONTINUOUS_AM", "2026-08-04T01:30:00Z", "2026-08-04T03:30:00Z"),
        ("CONTINUOUS_PM", "2026-08-04T05:00:00Z", "2026-08-04T07:00:00Z"),
    ]

    clock.import_day("SSE", "2026-08-04", "Asia/Shanghai", sessions)
    clock.import_day("SSE", "2026-08-04", "Asia/Shanghai", sessions)
    with pytest.raises(ValueError, match="calendar day conflicts"):
        clock.import_day(
            "SSE",
            "2026-08-04",
            "Asia/Shanghai",
            [("CONTINUOUS_AM", "2026-08-04T01:31:00Z", "2026-08-04T03:30:00Z")],
        )


def test_clock_calendar_coverage_readiness_requires_contiguous_dates_and_future_horizon(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m2_runtime
    clock = TradingClock(runtime, writer)
    first = date(2026, 8, 27)
    last = date(2026, 9, 3)

    current = first
    while current <= last:
        trading_date = current.isoformat()
        sessions = (
            [
                (
                    "CONTINUOUS_AM",
                    f"{trading_date}T01:30:00Z",
                    f"{trading_date}T03:30:00Z",
                ),
                (
                    "CONTINUOUS_PM",
                    f"{trading_date}T05:00:00Z",
                    f"{trading_date}T07:00:00Z",
                ),
            ]
            if current.weekday() < 5
            else []
        )
        clock.import_day("SSE", trading_date, "Asia/Shanghai", sessions)
        if current != date(2026, 8, 30):
            clock.import_day("SZSE", trading_date, "Asia/Shanghai", sessions)
        current += timedelta(days=1)

    ready = clock.calendar_coverage_readiness("SSE", datetime(2026, 8, 27, 6, tzinfo=UTC))
    assert ready.ready is True
    assert ready.state == "READY"
    assert ready.coverage_end == "2026-09-03"
    assert ready.future_trading_days == 5
    assert ready.missing_dates == ()

    too_close = clock.calendar_coverage_readiness("SSE", datetime(2026, 8, 28, 6, tzinfo=UTC))
    assert too_close.ready is False
    assert too_close.state == "CALENDAR_COVERAGE_INSUFFICIENT"
    assert too_close.future_trading_days == 4

    gap = clock.calendar_coverage_readiness("SZSE", datetime(2026, 8, 27, 6, tzinfo=UTC))
    assert gap.ready is False
    assert gap.state == "CALENDAR_COVERAGE_MISSING"
    assert gap.missing_dates == ("2026-08-30",)


def test_production_calendar_import_rerun_is_idempotent(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    from scripts.tdx_runner import _import_trading_calendar

    runtime, writer, _ = m2_runtime
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "calendars"
        / "sse-szse-2026-07-27-to-08-25.json"
    )

    first = _import_trading_calendar(path, runtime, writer)
    second = _import_trading_calendar(path, runtime, writer)

    assert first["days"] == 316
    assert first["inserted_days"] == 316
    assert second["inserted_days"] == 0
    with runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM trading_calendar_day").scalar_one()
            == 316
        )


def test_fixture_replay_is_deterministic_and_live_is_disabled(tmp_path: Path) -> None:
    fixture = tmp_path / "replay.json"
    fixture.write_text(
        json.dumps(
            {
                "batches": [
                    {
                        "id": "b1",
                        "received_at": "2026-08-04T01:30:01Z",
                        "records": [
                            {
                                "code": "600000",
                                "source_time": "2026-08-04T01:30:00Z",
                                "price": "10.25",
                                "volume": 100,
                            },
                            {"code": "GAP", "source_time": None, "price": None, "volume": None},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    first = list(FixtureReplayProvider(fixture).batches())
    second = list(FixtureReplayProvider(fixture).batches())
    assert first == second
    assert first[0].records[1].price is None
    with pytest.raises(ProviderDisabledError):
        list(DisabledLiveProvider().batches())
