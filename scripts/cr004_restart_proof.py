"""Run a deterministic, non-Shadow CR-004 historical restart proof."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
for source in reversed(
    (
        ROOT / "packages" / "data" / "src",
        ROOT / "packages" / "persistence" / "src",
    )
):
    sys.path.insert(0, str(source))

from market_monitor_data.clock import TradingClock  # noqa: E402
from market_monitor_data.health import SourceEpochService  # noqa: E402
from market_monitor_data.models import ProviderInstrumentRegistration  # noqa: E402
from market_monitor_data.reference import ReferenceRepository  # noqa: E402
from market_monitor_data.tdx.historical import TdxHistoricalLoader, WarmupResult  # noqa: E402
from market_monitor_data.tdx.models import TdxInstrument, TdxRawBar  # noqa: E402
from market_monitor_data.tdx.provider import TdxUniverse  # noqa: E402
from market_monitor_data.tdx.storage import TdxStorage  # noqa: E402
from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.values import format_rfc3339  # noqa: E402
from market_monitor_persistence.writer import WriterQueue  # noqa: E402

_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")
_AS_OF = datetime(2026, 8, 24, 1, 45, tzinfo=UTC)
_INSTRUMENTS = (
    TdxInstrument(1, "600000", "STOCK"),
    TdxInstrument(0, "000001", "STOCK"),
)
_PROTECTED_TABLES = (
    "analysis_subject",
    "analysis_commit",
    "evaluation_snapshot",
    "input_manifest",
    "state_evaluation",
    "state_transition",
    "current_state_projection",
    "guardian_evaluation",
    "scout_evaluation",
    "market_event",
    "event_version",
    "event_evidence",
    "current_event_projection",
    "notification_intent",
    "notification_delivery_state",
    "delivery_attempt",
    "threshold_activation",
)


class _FixtureGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str, int, int]] = []
        self._bars = {
            (instrument.market, instrument.code, "1m"): _minute_bars(instrument)
            for instrument in _INSTRUMENTS
        }
        self._bars.update(
            {
                (instrument.market, instrument.code, "1d"): _daily_bars(instrument)
                for instrument in _INSTRUMENTS
            }
        )

    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]:
        self.calls.append((instrument.market, instrument.code, interval, start, count))
        values = self._bars[(instrument.market, instrument.code, interval)]
        end = len(values) - start
        return values[max(0, end - count) : end]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    data_directory = arguments.data_dir.resolve()
    database_path = DatabasePaths.from_data_directory(data_directory).database_file
    if database_path.exists():
        raise SystemExit(f"proof data directory must be fresh: {data_directory}")

    first, epoch_uid = _seed_and_warm(data_directory)
    second, second_calls, migration_revision, integrity, ledger_rows, protected = _restart_and_warm(
        data_directory, epoch_uid
    )
    _require(first.state == "FIT", f"initial warm-up was {first.state}")
    _require(second.state == "FIT", f"restart warm-up was {second.state}")
    _require(second.fetch_requests == 0, "restart issued historical fetch requests")
    _require(not second_calls, "restart gateway was called")
    _require(integrity == "ok", f"integrity check was {integrity}")
    _require(
        all(value == 0 for value in protected.values()),
        f"protected tables changed: {protected}",
    )
    _require(migration_revision == "0014_cr003_official_cycle_journal", "unexpected migration")

    evidence = {
        "schema_version": 1,
        "proof": "CR-004_NON_SHADOW_RESTART",
        "data_directory": _relative_path(data_directory),
        "epoch_uid": epoch_uid,
        "observed_at": format_rfc3339(_AS_OF),
        "migration_revision": migration_revision,
        "initial": _warmup_record(first),
        "restart": _warmup_record(second),
        "restart_gateway_calls": len(second_calls),
        "ledger_rows": ledger_rows,
        "protected_table_counts": protected,
        "sqlite_integrity_check": integrity,
        "shadow_started": False,
        "official_started": False,
    }
    target = arguments.evidence_file.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


def _seed_and_warm(data_directory: Path) -> tuple[WarmupResult, str]:
    paths = DatabasePaths.from_data_directory(data_directory)
    runtime = DatabaseRuntime.open(paths)
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        clock = TradingClock(runtime, writer)
        _import_calendar(clock)
        references = ReferenceRepository(runtime, writer)
        artifacts = ArtifactStore(runtime, writer)
        listing = artifacts.put_bytes(
            b'{"proof":"cr004-restart-listing-fact"}',
            "application/vnd.market-monitor.reference+json",
        )
        artifacts.register(listing)
        valid_from = _AS_OF - timedelta(days=60)
        references.register_provider_instruments(
            "NATIVE_TDX",
            tuple(
                ProviderInstrumentRegistration(
                    f"{instrument.market}:{instrument.code}",
                    instrument.kind,
                    "SSE" if instrument.market == 1 else "SZSE",
                    instrument.code,
                    f"Restart proof {instrument.code}",
                    "LISTED",
                    "TRADING",
                    listing_effective_at=_AS_OF - timedelta(days=1),
                    listing_evidence_sha256=listing.sha256,
                    listing_source_ref="owner-approved-cr004-restart-proof",
                )
                for instrument in _INSTRUMENTS
            ),
            valid_from,
        )
        epoch_uid = SourceEpochService(runtime, writer).start_epoch(
            "NATIVE_TDX", "cr004-restart-proof", _AS_OF
        )
        gateway = _FixtureGateway()
        loader = TdxHistoricalLoader(TdxStorage(runtime, writer), gateway, clock)
        result = loader.warm(epoch_uid, TdxUniverse(_INSTRUMENTS, (), ()), _AS_OF)
        _require(result.fetch_requests == len(gateway.calls), "initial telemetry mismatch")
        return result, epoch_uid
    finally:
        writer.close()
        runtime.close()


def _restart_and_warm(
    data_directory: Path, epoch_uid: str
) -> tuple[
    WarmupResult,
    list[tuple[int, str, str, int, int]],
    str,
    str,
    list[dict[str, object]],
    dict[str, int],
]:
    paths = DatabasePaths.from_data_directory(data_directory)
    runtime = DatabaseRuntime.open(paths)
    manager = MigrationManager()
    manager.upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        clock = TradingClock(runtime, writer)
        gateway = _FixtureGateway()
        loader = TdxHistoricalLoader(TdxStorage(runtime, writer), gateway, clock)
        result = loader.warm(epoch_uid, TdxUniverse(_INSTRUMENTS, (), ()), _AS_OF)
        with runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT catchup_uid,status,coverage_checked,symbols_skipped,fetch_requests,"
                "fetched_bars,inserted_bars,duplicate_bars,missing_symbols,catchup_duration_ms "
                "FROM tdx_historical_catchup_run ORDER BY created_at,catchup_uid"
            ).all()
            ledger_rows = [
                {
                    "catchup_uid": str(row.catchup_uid),
                    "status": str(row.status),
                    "coverage_checked": int(row.coverage_checked),
                    "symbols_skipped": int(row.symbols_skipped),
                    "fetch_requests": int(row.fetch_requests),
                    "fetched_bars": int(row.fetched_bars),
                    "inserted_bars": int(row.inserted_bars),
                    "duplicate_bars": int(row.duplicate_bars),
                    "missing_symbols": int(row.missing_symbols),
                    "catchup_duration_ms": int(row.catchup_duration_ms),
                }
                for row in rows
            ]
            protected = {
                table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
                for table in _PROTECTED_TABLES
            }
            integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
        return result, gateway.calls, manager.verify(runtime), integrity, ledger_rows, protected
    finally:
        writer.close()
        runtime.close()


def _import_calendar(clock: TradingClock) -> None:
    dates = _previous_weekdays(_AS_OF.date(), 20) + (_AS_OF.date(),)
    for exchange in ("SSE", "SZSE"):
        for trading_date in dates:
            value = trading_date.isoformat()
            clock.import_day(
                exchange,
                value,
                "Asia/Shanghai",
                [
                    ("CONTINUOUS_AM", f"{value}T01:30:00Z", f"{value}T03:30:00Z"),
                    ("CONTINUOUS_PM", f"{value}T05:00:00Z", f"{value}T07:00:00Z"),
                ],
            )


def _minute_bars(instrument: TdxInstrument) -> tuple[TdxRawBar, ...]:
    current = tuple(
        _bar(instrument, datetime.combine(_AS_OF.date(), time(9, minute), _CHINA))
        for minute in range(30, 45)
    )
    prior = tuple(
        _bar(instrument, datetime.combine(trading_date, time(9, minute), _CHINA))
        for trading_date in _previous_weekdays(_AS_OF.date(), 5)
        for minute in range(40, 45)
    )
    return current + prior


def _daily_bars(instrument: TdxInstrument) -> tuple[TdxRawBar, ...]:
    return tuple(
        _bar(instrument, datetime.combine(trading_date, time(15), _CHINA))
        for trading_date in _previous_weekdays(_AS_OF.date(), 20)
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


def _warmup_record(result: WarmupResult) -> dict[str, object]:
    return {
        "catchup_uid": result.catchup_uid,
        "state": result.state,
        "coverage_ppm": result.coverage_ppm,
        "coverage_checked": result.coverage_checked,
        "symbols_skipped": result.symbols_skipped,
        "fetch_requests": result.fetch_requests,
        "fetched_bars": result.fetched_bars,
        "inserted_bars": result.inserted_bars,
        "duplicate_bars": result.duplicate_bars,
        "missing_symbols": result.missing_symbols,
        "catchup_duration_ms": result.catchup_duration_ms,
    }


def _relative_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
