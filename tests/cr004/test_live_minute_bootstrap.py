"""CR-004 current-day Native TDX legal-minute bootstrap."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import pytest
from market_monitor_data.clock import TradingClock
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderInstrumentRegistration
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import (
    TdxBlockDownload,
    TdxInstrument,
    TdxRawBar,
    TdxRawQuote,
    TdxSecurity,
)
from market_monitor_data.tdx.provider import NativeTdxProvider
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from scripts.tdx_runner import _bootstrap_current_live_tail

_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")
_RUNNING_CATCHUP_UID = "01a0569c-c002-7125-b2b1-643f95a05a28"
_PM_TARGET = datetime(2026, 8, 24, 14, 58, tzinfo=_CHINA).astimezone(UTC)


def _minutes(hour: int, first: int, last: int) -> tuple[datetime, ...]:
    return tuple(
        datetime(2026, 8, 24, hour, minute, tzinfo=_CHINA).astimezone(UTC)
        for minute in range(first, last + 1)
    )


class _Gateway:
    def __init__(
        self,
        tails: dict[int, tuple[datetime, ...]],
        *,
        missing: Collection[tuple[int, str, datetime]] = frozenset(),
        duplicate: Collection[tuple[int, str, datetime]] = frozenset(),
    ) -> None:
        self._tails = tails
        self._missing = missing
        self._duplicate = duplicate
        self.bar_calls: list[tuple[TdxInstrument, int, int]] = []

    def security_directory(self) -> tuple[TdxSecurity, ...]:
        return (
            TdxSecurity(0, "000001", "Shenzhen", "STOCK"),
            TdxSecurity(1, "600000", "Shanghai", "STOCK"),
        )

    def quotes(self, instruments: tuple[TdxInstrument, ...]) -> tuple[TdxRawQuote, ...]:
        raise AssertionError(f"quotes are not part of live-minute bootstrap: {instruments}")

    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]:
        assert interval == "1m"
        self.bar_calls.append((instrument, start, count))
        tail = self._tails[instrument.market]
        timestamps = [
            value
            for value in (*tail, tail[-1] + timedelta(minutes=1))
            if (instrument.market, instrument.code, value) not in self._missing
        ]
        timestamps.extend(
            value
            for value in tail
            if (instrument.market, instrument.code, value) in self._duplicate
        )
        return tuple(
            TdxRawBar(
                instrument.market,
                instrument.code,
                value,
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.5"),
                100,
                Decimal("1000"),
            )
            for value in timestamps
        )

    def block_file(self, filename: str) -> TdxBlockDownload:
        raise AssertionError(f"blocks are not part of live-minute bootstrap: {filename}")


@pytest.mark.parametrize(
    ("target", "expected"),
    (
        (
            _PM_TARGET,
            _minutes(14, 44, 58),
        ),
        (
            datetime(2026, 8, 24, 13, 5, tzinfo=_CHINA).astimezone(UTC),
            (*_minutes(11, 21, 30), *_minutes(13, 1, 5)),
        ),
        (
            datetime(2026, 8, 24, 9, 45, tzinfo=_CHINA).astimezone(UTC),
            _minutes(9, 31, 45),
        ),
        (
            datetime(2026, 8, 24, 9, 31, tzinfo=_CHINA).astimezone(UTC),
            _minutes(9, 31, 31),
        ),
    ),
)
def test_live_bootstrap_uses_latest_legal_completed_minute_tail(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    target: datetime,
    expected: tuple[datetime, ...],
) -> None:
    """Break if bootstrap uses natural lunch minutes or the 09:30 left edge."""
    clock = _clock(m3_runtime)

    assert clock.completed_legal_minute_tail("SSE", target, 15) == expected


def test_live_bootstrap_stores_exact_tail_and_reports_ready_without_historical_catchup(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """A FIT historical startup requests one bounded recent window per Primary symbol."""
    runtime, writer, artifacts = m3_runtime
    provider, _, epoch_uid, gateway, tails = _provider(m3_runtime)
    result = provider.bootstrap_primary_minute_tail(
        epoch_uid,
        {market: tail[-1] for market, tail in tails.items()},
        tails,
        _PM_TARGET + timedelta(seconds=30),
    )

    assert result.targets == ((0, _PM_TARGET), (1, _PM_TARGET))
    assert result.expected_count == 2
    assert result.known_suspended_count == 0
    assert result.valid_latest_complete_count == 2
    assert result.missing_or_invalid_count == 0
    assert result.coverage_ppm == 1_000_000
    assert result.health_capability == "HEALTHY"
    assert result.failure_counts == {"QUARANTINED": 0}
    assert all(
        start == 0 and count == len(tails[instrument.market]) + 2
        for instrument, start, count in gateway.bar_calls
    )
    with runtime.read_connection() as connection:
        stored = connection.exec_driver_sql(
            "SELECT count(*) FROM tdx_bar WHERE epoch_uid=? AND interval_kind='1m'", (epoch_uid,)
        ).scalar_one()
        catchups = connection.exec_driver_sql(
            "SELECT count(*) FROM tdx_historical_catchup_run"
        ).scalar_one()
    assert int(stored) == sum(len(tail) for tail in tails.values())
    assert int(catchups) == 0


def test_live_bootstrap_keeps_existing_limited_coverage_semantics(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """A frozen FIT_WITH_LIMITATIONS result is not silently upgraded to a 100% prerequisite."""
    missing = {(0, "000001", _PM_TARGET - timedelta(minutes=4))}
    provider, _, epoch_uid, _, tails = _provider(m3_runtime, missing=missing)
    result = provider.bootstrap_primary_minute_tail(
        epoch_uid,
        {market: tail[-1] for market, tail in tails.items()},
        tails,
        _PM_TARGET + timedelta(seconds=30),
    )

    assert result.valid_latest_complete_count == 1
    assert result.missing_or_invalid_count == 1
    assert result.coverage_ppm == 500_000
    assert result.health_capability == "DEGRADED"


def test_live_bootstrap_rejects_duplicate_required_bar(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Duplicate Native TDX data may not satisfy an exact legal-minute tail."""
    duplicate = {(0, "000001", _PM_TARGET - timedelta(minutes=4))}
    provider, _, epoch_uid, _, tails = _provider(m3_runtime, duplicate=duplicate)
    result = provider.bootstrap_primary_minute_tail(
        epoch_uid,
        {market: tail[-1] for market, tail in tails.items()},
        tails,
        _PM_TARGET + timedelta(seconds=30),
    )

    assert result.valid_latest_complete_count == 1
    assert result.missing_or_invalid_count == 1
    assert result.failure_counts == {"INVALID": 1, "QUARANTINED": 0}


def test_live_bootstrap_preserves_missing_tail_as_unfit_without_historical_catchup(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """A missing required legal minute remains fail-closed and never starts history paging."""
    runtime, _, _ = m3_runtime
    missing = {
        (0, "000001", _PM_TARGET - timedelta(minutes=4)),
        (1, "600000", _PM_TARGET - timedelta(minutes=4)),
    }
    provider, _, epoch_uid, _, tails = _provider(m3_runtime, missing=missing)
    result = provider.bootstrap_primary_minute_tail(
        epoch_uid,
        {market: tail[-1] for market, tail in tails.items()},
        tails,
        _PM_TARGET + timedelta(seconds=30),
    )

    assert result.valid_latest_complete_count == 0
    assert result.missing_or_invalid_count == 2
    assert result.coverage_ppm == 0
    assert result.health_capability == "UNHEALTHY"
    assert result.failure_counts == {"INCOMPLETE_TAIL": 2, "QUARANTINED": 0}
    with runtime.read_connection() as connection:
        catchups = connection.exec_driver_sql(
            "SELECT count(*) FROM tdx_historical_catchup_run"
        ).scalar_one()
    assert int(catchups) == 0


def test_live_bootstrap_runner_helper_fails_closed_when_tail_is_unfit(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """The runtime gate must not continue after an unfit bootstrap result."""
    missing = {
        (0, "000001", _PM_TARGET - timedelta(minutes=4)),
        (1, "600000", _PM_TARGET - timedelta(minutes=4)),
    }
    provider, clock, epoch_uid, _, _ = _provider(m3_runtime, missing=missing)

    with pytest.raises(ValueError, match="live minute tail is unfit"):
        _bootstrap_current_live_tail(provider, clock, epoch_uid, _PM_TARGET + timedelta(seconds=30))


def test_live_bootstrap_never_resumes_an_existing_running_historical_ledger(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """The erroneous historical RUNNING ledger stays untouched by live-tail bootstrap."""
    runtime, writer, _ = m3_runtime
    provider, _, epoch_uid, _, tails = _provider(m3_runtime)
    _seed_running_ledger(writer, epoch_uid)

    provider.bootstrap_primary_minute_tail(
        epoch_uid,
        {market: tail[-1] for market, tail in tails.items()},
        tails,
        _PM_TARGET + timedelta(seconds=30),
    )

    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT status,fetch_requests,fetched_bars,inserted_bars,duplicate_bars "
            "FROM tdx_historical_catchup_run WHERE catchup_uid=?",
            (_RUNNING_CATCHUP_UID,),
        ).one()
        count = connection.exec_driver_sql(
            "SELECT count(*) FROM tdx_historical_catchup_run"
        ).scalar_one()
    assert tuple(row) == ("RUNNING", 0, 0, 0, 0)
    assert int(count) == 1


def _provider(
    runtime_parts: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    *,
    missing: Collection[tuple[int, str, datetime]] = frozenset(),
    duplicate: Collection[tuple[int, str, datetime]] = frozenset(),
) -> tuple[
    NativeTdxProvider,
    TradingClock,
    str,
    _Gateway,
    dict[int, tuple[datetime, ...]],
]:
    runtime, writer, artifacts = runtime_parts
    clock = _clock(runtime_parts)
    tails = {
        0: clock.completed_legal_minute_tail("SZSE", _PM_TARGET, 15),
        1: clock.completed_legal_minute_tail("SSE", _PM_TARGET, 15),
    }
    gateway = _Gateway(tails, missing=missing, duplicate=duplicate)
    references = ReferenceRepository(runtime, writer)
    artifact = artifacts.put_bytes(b'{"fixture":"live-minute-bootstrap"}', "application/json")
    artifacts.register(artifact)
    references.register_provider_instruments(
        "NATIVE_TDX",
        tuple(
            ProviderInstrumentRegistration(
                f"{security.market}:{security.code}",
                security.kind,
                "SSE" if security.market == 1 else "SZSE",
                security.code,
                security.name,
                "LISTED",
                security.trading_status,
                _PM_TARGET - timedelta(days=1),
                artifact.sha256,
                "fixture-live-minute-bootstrap",
            )
            for security in gateway.security_directory()
        ),
        _PM_TARGET - timedelta(minutes=1),
    )
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 40_000, 60)),
        TdxStorage(runtime, writer),
        gateway,
        clock=clock,
    )
    universe = provider.synchronize_reference(_PM_TARGET)
    assert len(universe.primary) == 2
    epoch_uid = SourceEpochService(runtime, writer).start_epoch(
        "NATIVE_TDX", "live-minute-bootstrap", _PM_TARGET
    )
    return provider, clock, epoch_uid, gateway, tails


def _clock(
    runtime_parts: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> TradingClock:
    runtime, writer, _ = runtime_parts
    clock = TradingClock(runtime, writer)
    for exchange in ("SSE", "SZSE"):
        clock.import_day(
            exchange,
            "2026-08-24",
            "Asia/Shanghai",
            [
                ("CONTINUOUS_AM", "2026-08-24T01:30:00Z", "2026-08-24T03:30:00Z"),
                ("CONTINUOUS_PM", "2026-08-24T05:00:00Z", "2026-08-24T07:00:00Z"),
            ],
        )
    return clock


def _seed_running_ledger(writer: WriterQueue, epoch_uid: str) -> None:
    observed_at = format_rfc3339(_PM_TARGET)

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO tdx_historical_catchup_run("
            "catchup_uid,epoch_uid,observed_at,status,coverage_checked,symbols_skipped,"
            "fetch_requests,fetched_bars,inserted_bars,duplicate_bars,missing_symbols,"
            "catchup_duration_ms,created_at,updated_at) "
            "VALUES (?,?,?,'RUNNING',2,0,0,0,0,0,2,0,?,?)",
            (_RUNNING_CATCHUP_UID, epoch_uid, observed_at, observed_at, observed_at),
        )

    writer.submit(command).result()
