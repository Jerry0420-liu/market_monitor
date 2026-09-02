from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import pytest
from market_monitor_data.clock import TradingClock
from market_monitor_data.health import SourceEpochService
from market_monitor_data.models import ProviderInstrumentRegistration
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.historical import TdxHistoricalLoader
from market_monitor_data.tdx.models import TdxBar, TdxInstrument, TdxRawBar
from market_monitor_data.tdx.provider import TdxUniverse
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.writer import TransactionContext

CN = timezone(timedelta(hours=8), "Asia/Shanghai")
AS_OF = datetime(2026, 8, 24, 1, 45, tzinfo=UTC)
PM_AS_OF = datetime(2026, 8, 24, 6, 58, tzinfo=UTC)


def test_tdx_quote_time_requires_a_valid_trading_session(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if a raw TDX HHMMSS can become canonical outside a market session."""
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
    received_at = datetime(2026, 8, 24, 1, 35, tzinfo=UTC)

    assert clock.tdx_quote_time("SSE", received_at, 93000) == "2026-08-24T01:30:00.000000Z"
    assert clock.tdx_quote_time("SSE", received_at, 120000) is None
    assert clock.tdx_quote_time("SSE", received_at, 246000) is None


def test_tdx_quote_time_accepts_native_hhmmss_centiseconds(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Break if real Native TDX HHMMSScc time loses its valid source-time lineage."""
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

    assert (
        clock.tdx_quote_time("SSE", datetime(2026, 8, 24, 1, 35, tzinfo=UTC), 9300012)
        == "2026-08-24T01:30:00.120000Z"
    )


def test_warmup_uses_five_prior_valid_dates_at_same_clock_not_recent_windows(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if turnover warm-up substitutes prior intraday windows for prior trading days."""
    loader, epoch_uid, universe, _ = _setup(m3_runtime)

    result = loader.warm(epoch_uid, universe, AS_OF)

    assert result.same_clock_trading_dates == (
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
    )
    assert result.minute_complete == len(universe.primary)
    assert result.daily_complete == len(universe.primary)
    assert result.coverage_ppm == 1_000_000
    assert result.state == "FIT"


def test_read_only_assessment_of_fit_history_creates_no_catchup_or_tdx_call(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Qualification readiness must not turn a FIT restart into maintenance."""
    runtime, _, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)
    assert loader.warm(epoch_uid, universe, AS_OF).state == "FIT"  # explicit maintenance setup
    calls_before = len(gateway.calls)
    with runtime.read_connection() as connection:
        ledgers_before = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM tdx_historical_catchup_run"
            ).scalar_one()
        )
        bars_before = int(connection.exec_driver_sql("SELECT count(*) FROM tdx_bar").scalar_one())

    result = loader.assess(epoch_uid, universe, AS_OF)

    with runtime.read_connection() as connection:
        ledgers_after = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM tdx_historical_catchup_run"
            ).scalar_one()
        )
        bars_after = int(connection.exec_driver_sql("SELECT count(*) FROM tdx_bar").scalar_one())
    assert result.state == "FIT"
    assert (
        result.fetch_requests,
        result.fetched_bars,
        result.inserted_bars,
        result.duplicate_bars,
    ) == (
        0,
        0,
        0,
        0,
    )
    assert len(gateway.calls) == calls_before
    assert (ledgers_after, bars_after) == (ledgers_before, bars_before)


def test_read_only_assessment_of_missing_history_fails_without_repair(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """A non-FIT qualification gate must not silently repair historical history."""
    runtime, _, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)
    with runtime.read_connection() as connection:
        ledgers_before = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM tdx_historical_catchup_run"
            ).scalar_one()
        )
        bars_before = int(connection.exec_driver_sql("SELECT count(*) FROM tdx_bar").scalar_one())

    result = loader.assess(epoch_uid, universe, AS_OF)

    with runtime.read_connection() as connection:
        ledgers_after = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM tdx_historical_catchup_run"
            ).scalar_one()
        )
        bars_after = int(connection.exec_driver_sql("SELECT count(*) FROM tdx_bar").scalar_one())
    assert result.state == "WARMING_UP"
    assert result.missing_symbols == len(universe.primary)
    assert (
        result.fetch_requests,
        result.fetched_bars,
        result.inserted_bars,
        result.duplicate_bars,
    ) == (
        0,
        0,
        0,
        0,
    )
    assert gateway.calls == []
    assert (ledgers_after, bars_after) == (ledgers_before, bars_before)


def test_pm_historical_readiness_excludes_missing_current_day_live_tail(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if PM startup backfills the current-day live tail as history."""
    runtime, writer, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)
    storage = TdxStorage(runtime, writer)
    prior_days = _previous_weekdays(PM_AS_OF.date(), 5)
    daily_days = _previous_weekdays(PM_AS_OF.date(), 20)
    for instrument in universe.primary:
        storage.record_bars(
            epoch_uid,
            tuple(
                TdxBar(
                    instrument,
                    "1m",
                    datetime.combine(trading_day, datetime.min.time(), CN).replace(
                        hour=14, minute=minute
                    ),
                    Decimal("10"),
                    Decimal("11"),
                    Decimal("9"),
                    Decimal("10.5"),
                    100,
                    "SHARES",
                    Decimal("1000"),
                )
                for trading_day in prior_days
                for minute in range(54, 59)
            )
            + tuple(
                TdxBar(
                    instrument,
                    "1d",
                    datetime.combine(trading_day, datetime.min.time(), CN).replace(hour=15),
                    Decimal("10"),
                    Decimal("11"),
                    Decimal("9"),
                    Decimal("10.5"),
                    100,
                    "SHARES",
                    Decimal("1000"),
                )
                for trading_day in daily_days
            ),
        )

    result = loader.warm(epoch_uid, universe, PM_AS_OF)

    assert result.state == "FIT"
    assert result.fetch_requests == 0
    assert gateway.calls == []
    current_pm_start = datetime(2026, 8, 24, 13, 1, tzinfo=CN)
    current_pm_end = datetime(2026, 8, 24, 14, 58, tzinfo=CN)
    assert not any(
        current_pm_start <= bar.timestamp.astimezone(CN) <= current_pm_end
        for instrument in universe.primary
        for bar in storage.bars(epoch_uid, instrument, "1m")
    )


@pytest.mark.parametrize(
    ("missing_minute", "missing_same_day", "daily_count"),
    [
        (datetime(2026, 8, 21, 9, 43, tzinfo=CN), None, 20),
        (None, date(2026, 8, 20), 20),
        (None, None, 19),
    ],
)
def test_warmup_stays_warming_up_when_required_history_is_incomplete(
    m3_runtime: tuple[Any, Any, Any],
    missing_minute: datetime | None,
    missing_same_day: date | None,
    daily_count: int,
) -> None:
    """Fail if an incomplete minute, date, or daily window is silently treated as complete."""
    loader, epoch_uid, universe, _ = _setup(
        m3_runtime,
        missing_minute=missing_minute,
        missing_same_day=missing_same_day,
        daily_count=daily_count,
    )

    result = loader.warm(epoch_uid, universe, AS_OF)

    assert result.state == "WARMING_UP"
    assert result.coverage_ppm < 1_000_000
    assert result.fetch_requests > 0


def test_warmup_fails_closed_on_transport_error(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if an unavailable range becomes synthetic historical coverage."""
    loader, epoch_uid, universe, _ = _setup(m3_runtime, fail=True)

    result = loader.warm(epoch_uid, universe, AS_OF)

    assert result.state == "WARMING_UP"
    assert result.coverage_ppm == 0


def test_warmup_without_an_imported_trading_calendar_stays_warming_up(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if absent valid-day lineage is treated as a complete five-day window."""
    runtime, writer, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)

    def remove_calendar(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql("DELETE FROM trading_session")
        transaction.connection.exec_driver_sql("DELETE FROM trading_calendar_day")

    writer.submit(remove_calendar).result()
    result = loader.warm(epoch_uid, universe, AS_OF)

    assert result.state == "WARMING_UP"
    assert result.coverage_ppm == 0
    assert gateway.calls == []


def test_warmup_resumes_from_stored_canonical_coverage(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if a FIT restart re-fetches or cannot account for complete canonical history."""
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)

    assert loader.warm(epoch_uid, universe, AS_OF).state == "FIT"
    calls_after_first_warmup = len(gateway.calls)
    restart = loader.warm(epoch_uid, universe, AS_OF)

    assert restart.state == "FIT"
    assert len(gateway.calls) == calls_after_first_warmup
    assert restart.coverage_checked == len(universe.primary)
    assert restart.symbols_skipped == len(universe.primary)
    assert restart.fetch_requests == 0
    assert restart.fetched_bars == 0
    assert restart.inserted_bars == 0
    assert restart.duplicate_bars == 0
    assert restart.missing_symbols == 0
    assert restart.catchup_duration_ms >= 0
    with m3_runtime[0].read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT status,coverage_checked,symbols_skipped,fetch_requests,fetched_bars,"
            "inserted_bars,duplicate_bars,missing_symbols,catchup_duration_ms "
            "FROM tdx_historical_catchup_run WHERE catchup_uid=?",
            (restart.catchup_uid,),
        ).one()
    assert tuple(row) == (
        "FIT",
        len(universe.primary),
        len(universe.primary),
        0,
        0,
        0,
        0,
        0,
        restart.catchup_duration_ms,
    )


def test_restart_coverage_uses_exact_persisted_index_not_full_bar_series(
    m3_runtime: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail if a FIT restart reads every stored bar instead of indexed required-window coverage."""
    loader, epoch_uid, universe, _ = _setup(m3_runtime)

    assert loader.warm(epoch_uid, universe, AS_OF).state == "FIT"

    def unexpected_full_series_read(*_: object, **__: object) -> tuple[object, ...]:
        raise AssertionError("FIT restart must use exact coverage reads")

    monkeypatch.setattr(loader._storage, "bars", unexpected_full_series_read)
    result = loader.warm(epoch_uid, universe, AS_OF)

    assert result.state == "FIT"
    assert result.fetch_requests == 0


def test_catchup_telemetry_distinguishes_missing_fetch_insert_and_duplicate_bars(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if a bounded catch-up cannot distinguish a real gap from repeated page transport."""
    runtime, writer, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)
    instrument = universe.primary[0]

    assert loader.warm(epoch_uid, universe, AS_OF).state == "FIT"

    def remove_one_required_bar(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "DELETE FROM tdx_bar WHERE epoch_uid=? AND interval_kind='1m' "
            "AND source_time=? AND instrument_uid=("
            "SELECT instrument_uid FROM provider_mapping "
            "WHERE provider_key='NATIVE_TDX' AND external_code=? "
            "ORDER BY valid_from DESC LIMIT 1)",
            (
                epoch_uid,
                "2026-08-21T01:44:00.000000Z",
                f"{instrument.market}:{instrument.code}",
            ),
        )

    writer.submit(remove_one_required_bar).result()
    gateway.calls.clear()
    result = loader.warm(epoch_uid, universe, AS_OF)

    assert result.state == "FIT"
    assert {(market, code) for market, code, _, _, _ in gateway.calls} == {
        (instrument.market, instrument.code)
    }
    assert result.coverage_checked == len(universe.primary)
    assert result.missing_symbols == 1
    assert result.fetch_requests == len(gateway.calls)
    assert result.fetched_bars > result.inserted_bars == 1
    assert result.duplicate_bars == result.fetched_bars - result.inserted_bars
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT coverage_checked,symbols_skipped,fetch_requests,fetched_bars,inserted_bars,"
            "duplicate_bars,missing_symbols,catchup_duration_ms "
            "FROM tdx_historical_catchup_run WHERE catchup_uid=?",
            (result.catchup_uid,),
        ).one()
    assert tuple(int(value) for value in row) == (
        len(universe.primary),
        len(universe.primary) - 1,
        result.fetch_requests,
        result.fetched_bars,
        result.inserted_bars,
        result.duplicate_bars,
        1,
        result.catchup_duration_ms,
    )


def test_interrupted_catchup_retains_checkpoint_without_shadow_or_official_side_effects(
    m3_runtime: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process interruption leaves resumable evidence and no analysis-side write."""
    runtime, _, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)
    guarded_tables = (
        "evaluation_snapshot",
        "current_state_projection",
        "state_transition",
        "market_event",
        "notification_intent",
        "delivery_attempt",
        "threshold_activation",
    )
    with runtime.read_connection() as connection:
        before = {
            table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
            for table in guarded_tables
        }

    def interrupt(*_: object, **__: object) -> tuple[TdxRawBar, ...]:
        raise KeyboardInterrupt

    monkeypatch.setattr(gateway, "bars", interrupt)
    with pytest.raises(KeyboardInterrupt):
        loader.warm(epoch_uid, universe, AS_OF)

    with runtime.read_connection() as connection:
        checkpoint = connection.exec_driver_sql(
            "SELECT status,coverage_checked,symbols_skipped,fetch_requests,fetched_bars,"
            "inserted_bars,duplicate_bars,missing_symbols FROM tdx_historical_catchup_run "
            "ORDER BY created_at DESC,catchup_uid DESC LIMIT 1"
        ).one()
        after = {
            table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
            for table in guarded_tables
        }
    assert tuple(checkpoint) == (
        "RUNNING",
        len(universe.primary),
        0,
        1,
        0,
        0,
        0,
        len(universe.primary),
    )
    assert after == before


def test_unexpected_catchup_failure_finalizes_failed_ledger(
    m3_runtime: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary exception is durable as FAILED; process interruption remains RUNNING."""
    runtime, _, _ = m3_runtime
    loader, epoch_uid, universe, gateway = _setup(m3_runtime)

    def fail_after_start(*_: object, **__: object) -> tuple[TdxRawBar, ...]:
        raise AssertionError("fixture failure")

    monkeypatch.setattr(gateway, "bars", fail_after_start)
    with pytest.raises(AssertionError, match="fixture failure"):
        loader.warm(epoch_uid, universe, AS_OF)

    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT status,coverage_checked,symbols_skipped,fetch_requests,fetched_bars,"
            "inserted_bars,duplicate_bars,missing_symbols "
            "FROM tdx_historical_catchup_run ORDER BY created_at DESC,catchup_uid DESC LIMIT 1"
        ).one()
    assert tuple(row) == (
        "FAILED",
        len(universe.primary),
        0,
        1,
        0,
        0,
        0,
        len(universe.primary),
    )


class _Gateway:
    def __init__(self, bars: dict[tuple[int, str, str], tuple[TdxRawBar, ...]], fail: bool) -> None:
        self._bars = bars
        self._fail = fail
        self.calls: list[tuple[int, str, str, int, int]] = []

    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]:
        self.calls.append((instrument.market, instrument.code, interval, start, count))
        if self._fail:
            raise OSError("fixture transport failure")
        values = self._bars[(instrument.market, instrument.code, interval)]
        end = len(values) - start
        return values[max(0, end - count) : end]


def _setup(
    m3_runtime: tuple[Any, Any, Any],
    *,
    missing_minute: datetime | None = None,
    missing_same_day: date | None = None,
    daily_count: int = 20,
    fail: bool = False,
) -> tuple[TdxHistoricalLoader, str, TdxUniverse, _Gateway]:
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    trading_days = _previous_weekdays(AS_OF.date(), 20) + (AS_OF.date(),)
    for exchange in ("SSE", "SZSE"):
        for trading_day in trading_days:
            _import_cn_sessions(clock, exchange, trading_day)
    instruments = tuple(
        TdxInstrument(1 if index % 2 else 0, f"{600000 + index:06d}", "STOCK") for index in range(5)
    )
    references = ReferenceRepository(runtime, writer)
    registered_at = datetime(2026, 1, 1, tzinfo=UTC)
    for instrument in instruments:
        references.register_provider_instruments(
            "NATIVE_TDX",
            (
                ProviderInstrumentRegistration(
                    f"{instrument.market}:{instrument.code}",
                    "STOCK",
                    "SSE" if instrument.market == 1 else "SZSE",
                    instrument.code,
                    instrument.code,
                    "LISTED",
                    "TRADING",
                ),
            ),
            registered_at,
        )
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "warmup", AS_OF)
    gateway = _Gateway(
        {
            (instrument.market, instrument.code, "1m"): _minute_bars(
                instrument, missing_minute, missing_same_day
            )
            for instrument in instruments
        }
        | {
            (instrument.market, instrument.code, "1d"): _daily_bars(instrument, daily_count)
            for instrument in instruments
        },
        fail,
    )
    return (
        TdxHistoricalLoader(TdxStorage(runtime, writer), gateway, clock),
        epoch_uid,
        TdxUniverse(instruments, (), ()),
        gateway,
    )


def _minute_bars(
    instrument: TdxInstrument,
    missing_minute: datetime | None,
    missing_same_day: date | None,
) -> tuple[TdxRawBar, ...]:
    bars = [
        _bar(instrument, datetime(2026, 8, 24, 9, minute, tzinfo=CN)) for minute in range(31, 46)
    ]
    for trading_day in _previous_weekdays(AS_OF.date(), 5):
        if trading_day == missing_same_day:
            continue
        bars.extend(
            _bar(
                instrument,
                datetime.combine(trading_day, datetime.min.time(), CN).replace(
                    hour=9, minute=minute
                ),
            )
            for minute in range(41, 46)
        )
    return tuple(bar for bar in bars if bar.timestamp != missing_minute)


def _daily_bars(instrument: TdxInstrument, count: int) -> tuple[TdxRawBar, ...]:
    return tuple(
        _bar(
            instrument,
            datetime.combine(trading_day, datetime.min.time(), CN).replace(hour=15),
        )
        for trading_day in _previous_weekdays(AS_OF.date(), 20)[-count:]
    )


def _bar(instrument: TdxInstrument, timestamp: datetime) -> TdxRawBar:
    return TdxRawBar(
        instrument.market,
        instrument.code,
        timestamp,
        Decimal("10"),
        Decimal("11"),
        Decimal("9"),
        Decimal("10.5"),
        100,
        Decimal("1000"),
    )


def _previous_weekdays(before: date, count: int) -> tuple[date, ...]:
    values: list[date] = []
    candidate = before
    while len(values) < count:
        candidate -= timedelta(days=1)
        if candidate.weekday() < 5:
            values.append(candidate)
    return tuple(reversed(values))


def _import_cn_sessions(clock: TradingClock, exchange: str, trading_day: date) -> None:
    value = trading_day.isoformat()
    clock.import_day(
        exchange,
        value,
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", f"{value}T01:30:00Z", f"{value}T03:30:00Z"),
            ("CONTINUOUS_PM", f"{value}T05:00:00Z", f"{value}T07:00:00Z"),
        ],
    )
