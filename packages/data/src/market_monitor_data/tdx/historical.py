from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from time import monotonic_ns
from typing import TYPE_CHECKING, Literal, Protocol

from market_monitor_data.clock import TradingClock
from market_monitor_data.tdx.models import TdxBar, TdxInstrument, TdxRawBar
from market_monitor_data.tdx.normalization import TdxInvalidRecord, normalize_bar
from market_monitor_data.tdx.storage import TdxStorage

if TYPE_CHECKING:
    from market_monitor_data.tdx.provider import TdxUniverse


_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")


class _BarsGateway(Protocol):
    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]: ...


@dataclass(frozen=True)
class WarmupResult:
    minute_complete: int
    daily_complete: int
    same_clock_trading_dates: tuple[str, ...]
    coverage_ppm: int
    state: str
    catchup_uid: str
    coverage_checked: int
    symbols_skipped: int
    fetch_requests: int
    fetched_bars: int
    inserted_bars: int
    duplicate_bars: int
    missing_symbols: int
    catchup_duration_ms: int


@dataclass
class _CatchupCounters:
    fetch_requests: int = 0
    fetched_bars: int = 0
    inserted_bars: int = 0
    duplicate_bars: int = 0


class TdxHistoricalLoader:
    """Check persisted CR-004 coverage first, then fetch only bounded missing ranges."""

    _MINUTE_PAGE_SIZE = 800
    _DAILY_COUNT = 21

    def __init__(self, storage: TdxStorage, gateway: _BarsGateway, clock: TradingClock) -> None:
        self._storage = storage
        self._gateway = gateway
        self._clock = clock

    def assess(self, epoch_uid: str, universe: TdxUniverse, observed_at: datetime) -> WarmupResult:
        """Read persisted historical readiness without creating a catch-up ledger."""
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("warm-up time must be timezone-aware")
        started_ns = monotonic_ns()
        trading_date = observed_at.astimezone(_CHINA).date().isoformat()
        dates = {
            exchange: self._clock.previous_valid_trading_dates(exchange, trading_date, 5)
            for exchange in ("SSE", "SZSE")
        }
        daily_dates = {
            exchange: self._clock.previous_valid_trading_dates(exchange, trading_date, 20)
            for exchange in ("SSE", "SZSE")
        }
        required_exchanges = {_exchange(instrument) for instrument in universe.primary}
        calendar_ready = bool(universe.primary) and not any(
            len(dates[exchange]) != 5 or len(daily_dates[exchange]) != 20
            for exchange in required_exchanges
        )
        minute_requirements: dict[int, tuple[datetime, ...]] = {}
        daily_requirements: dict[int, tuple[str, ...]] = {}
        if calendar_ready:
            for market in sorted({instrument.market for instrument in universe.primary}):
                exchange = "SSE" if market == 1 else "SZSE"
                target = self._clock.latest_completed_legal_minute(exchange, observed_at)
                if target is None:
                    calendar_ready = False
                    break
                local_target = target.astimezone(_CHINA)
                minute_requirements[market] = tuple(
                    sorted(
                        timestamp
                        for value in dates[exchange]
                        for timestamp in _window_timestamps(
                            value, local_target.hour, local_target.minute
                        )
                    )
                )
                daily_requirements[market] = daily_dates[exchange]
        expected = {(item.market, item.code) for item in universe.primary}
        coverage = (
            self._storage.historical_coverage(
                epoch_uid, universe.primary, minute_requirements, daily_requirements
            )
            if calendar_ready
            else None
        )
        minute_complete = frozenset() if coverage is None else coverage.minute_complete
        daily_complete = frozenset() if coverage is None else coverage.daily_complete
        complete = minute_complete & daily_complete
        total = len(universe.primary)
        coverage_ppm = len(complete) * 1_000_000 // total if total else 0
        return WarmupResult(
            len(minute_complete),
            len(daily_complete),
            dates["SSE"],
            coverage_ppm,
            "FIT" if coverage_ppm >= 800_000 else "WARMING_UP",
            "",
            total,
            len(complete),
            0,
            0,
            0,
            0,
            len(expected - complete),
            _elapsed_ms(started_ns),
        )

    def warm(self, epoch_uid: str, universe: TdxUniverse, observed_at: datetime) -> WarmupResult:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("warm-up time must be timezone-aware")
        started_ns = monotonic_ns()
        trading_date = observed_at.astimezone(_CHINA).date().isoformat()
        dates = {
            exchange: self._clock.previous_valid_trading_dates(exchange, trading_date, 5)
            for exchange in ("SSE", "SZSE")
        }
        daily_dates = {
            exchange: self._clock.previous_valid_trading_dates(exchange, trading_date, 20)
            for exchange in ("SSE", "SZSE")
        }
        required_exchanges = {_exchange(instrument) for instrument in universe.primary}
        calendar_ready = bool(universe.primary) and not any(
            len(dates[exchange]) != 5 or len(daily_dates[exchange]) != 20
            for exchange in required_exchanges
        )
        minute_requirements: dict[int, tuple[datetime, ...]] = {}
        daily_requirements: dict[int, tuple[str, ...]] = {}
        if calendar_ready:
            for market in sorted({instrument.market for instrument in universe.primary}):
                exchange = "SSE" if market == 1 else "SZSE"
                target = self._clock.latest_completed_legal_minute(exchange, observed_at)
                if target is None:
                    calendar_ready = False
                    break
                local_target = target.astimezone(_CHINA)
                prior_windows = {
                    timestamp
                    for value in dates[exchange]
                    for timestamp in _window_timestamps(
                        value, local_target.hour, local_target.minute
                    )
                }
                minute_requirements[market] = tuple(sorted(prior_windows))
                daily_requirements[market] = daily_dates[exchange]

        expected = {(item.market, item.code) for item in universe.primary}
        initial = (
            self._storage.historical_coverage(
                epoch_uid, universe.primary, minute_requirements, daily_requirements
            )
            if calendar_ready
            else None
        )
        initial_complete = frozenset() if initial is None else initial.complete
        initial_missing = expected - initial_complete
        catchup_uid = self._storage.start_historical_catchup(
            epoch_uid,
            observed_at,
            coverage_checked=len(universe.primary),
            symbols_skipped=len(initial_complete),
            missing_symbols=len(initial_missing),
        )
        counters = _CatchupCounters()
        try:
            if calendar_ready and initial is not None:
                active_quarantine = self._storage.active_quarantine(epoch_uid, observed_at)
                for instrument in universe.primary:
                    identity = (instrument.market, instrument.code)
                    if identity not in initial_missing or identity in active_quarantine:
                        continue
                    if identity not in initial.minute_complete:
                        self._fetch(
                            epoch_uid,
                            instrument,
                            "1m",
                            observed_at,
                            catchup_uid,
                            counters,
                            started_ns,
                        )
                    if identity not in initial.daily_complete:
                        self._fetch(
                            epoch_uid,
                            instrument,
                            "1d",
                            observed_at,
                            catchup_uid,
                            counters,
                            started_ns,
                        )

            final = (
                self._storage.historical_coverage(
                    epoch_uid, universe.primary, minute_requirements, daily_requirements
                )
                if calendar_ready
                else None
            )
            minute_complete = 0 if final is None else len(final.minute_complete)
            daily_complete = 0 if final is None else len(final.daily_complete)
            complete = 0 if final is None else len(final.complete)
            total = len(universe.primary)
            coverage_ppm = complete * 1_000_000 // total if total else 0
            state = "FIT" if coverage_ppm >= 800_000 else "WARMING_UP"
            duration_ms = _elapsed_ms(started_ns)
            self._storage.checkpoint_historical_catchup(
                catchup_uid,
                observed_at,
                status=state,
                fetch_requests=counters.fetch_requests,
                fetched_bars=counters.fetched_bars,
                inserted_bars=counters.inserted_bars,
                duplicate_bars=counters.duplicate_bars,
                catchup_duration_ms=duration_ms,
            )
            return WarmupResult(
                minute_complete,
                daily_complete,
                dates["SSE"],
                coverage_ppm,
                state,
                catchup_uid,
                total,
                len(initial_complete),
                counters.fetch_requests,
                counters.fetched_bars,
                counters.inserted_bars,
                counters.duplicate_bars,
                len(initial_missing),
                duration_ms,
            )
        except Exception:
            # Preserve durable counters while distinguishing failure from a killed process.
            try:
                self._storage.checkpoint_historical_catchup(
                    catchup_uid,
                    observed_at,
                    status="FAILED",
                    fetch_requests=counters.fetch_requests,
                    fetched_bars=counters.fetched_bars,
                    inserted_bars=counters.inserted_bars,
                    duplicate_bars=counters.duplicate_bars,
                    catchup_duration_ms=_elapsed_ms(started_ns),
                )
            except Exception:
                pass
            raise

    def _fetch(
        self,
        epoch_uid: str,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        observed_at: datetime,
        catchup_uid: str,
        counters: _CatchupCounters,
        started_ns: int,
    ) -> None:
        count = self._MINUTE_PAGE_SIZE if interval == "1m" else self._DAILY_COUNT
        starts = (0, self._MINUTE_PAGE_SIZE) if interval == "1m" else (0,)
        for start in starts:
            counters.fetch_requests += 1
            self._checkpoint(catchup_uid, observed_at, counters, started_ns)
            try:
                raw_bars = self._gateway.bars(instrument, interval, start=start, count=count)
                normalized = [normalize_bar(raw, instrument, interval) for raw in raw_bars]
                invalid = next(
                    (item for item in normalized if isinstance(item, TdxInvalidRecord)), None
                )
                if invalid is not None:
                    raise ValueError(invalid.reason)
            except OSError, RuntimeError, ValueError:
                self._storage.record_quarantine(
                    epoch_uid,
                    instrument.market,
                    instrument.code,
                    "HISTORICAL_RANGE_FAILURE",
                    observed_at,
                    observed_at + timedelta(minutes=1),
                )
                self._checkpoint(catchup_uid, observed_at, counters, started_ns)
                return
            bars = tuple(item for item in normalized if isinstance(item, TdxBar))
            counters.fetched_bars += len(bars)
            self._checkpoint(catchup_uid, observed_at, counters, started_ns)
            inserted = self._storage.record_bars(epoch_uid, bars) if bars else 0
            counters.inserted_bars += inserted
            counters.duplicate_bars += len(bars) - inserted
            self._checkpoint(catchup_uid, observed_at, counters, started_ns)
            if len(raw_bars) < count:
                break

    def _checkpoint(
        self,
        catchup_uid: str,
        observed_at: datetime,
        counters: _CatchupCounters,
        started_ns: int,
    ) -> None:
        self._storage.checkpoint_historical_catchup(
            catchup_uid,
            observed_at,
            status="RUNNING",
            fetch_requests=counters.fetch_requests,
            fetched_bars=counters.fetched_bars,
            inserted_bars=counters.inserted_bars,
            duplicate_bars=counters.duplicate_bars,
            catchup_duration_ms=_elapsed_ms(started_ns),
        )


def _current_window(
    clock: TradingClock, exchange: str, target: datetime | None
) -> tuple[datetime, ...]:
    if target is None:
        return ()
    session = clock.continuous_session_at(exchange, target - timedelta(microseconds=1))
    if session is None:
        return ()
    values = tuple(target - timedelta(minutes=offset) for offset in range(14, -1, -1))
    return values if values[0] > session[1] else ()


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (monotonic_ns() - started_ns) // 1_000_000)


def _window_timestamps(trading_date: str, hour: int, minute: int) -> set[datetime]:
    end = datetime.combine(date.fromisoformat(trading_date), time(hour, minute), _CHINA).astimezone(
        UTC
    )
    return {end - timedelta(minutes=offset) for offset in range(5)}


def _exchange(instrument: TdxInstrument) -> str:
    return "SSE" if instrument.market == 1 else "SZSE"
