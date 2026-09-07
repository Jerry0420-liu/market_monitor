"""Production worker wiring for the existing CR-003 orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Event

from market_monitor_analysis.official_orchestrator import (
    OfficialOrchestrator,
    OfficialOrchestratorConfig,
    SqliteCycleJournal,
)
from market_monitor_analysis.production_worker import ContinuousProductionWorker
from market_monitor_api.runtime_lock import RuntimeLock
from market_monitor_data.clock import TradingClock
from market_monitor_data.tdx.client import TdxLiveClient
from market_monitor_data.tdx.protocol import TdxProtocol
from market_monitor_data.tdx.transport import TdxServerPool
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.maintenance import (
    apply_tdx_bar_retention,
    plan_tdx_bar_retention,
)
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue

from scripts.official_runner import NativeTdxOfficialPipeline, OfficialRuntimeSettings

_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")
_DEFAULT_BAR_RETENTION_TRADING_DAYS = 5
_MAX_BAR_RETENTION_TRADING_DAYS = 30
_logger = logging.getLogger(__name__)


class NativeTdxCycleRunner:
    """Bind one existing official pipeline to a long-lived worker process."""

    def __init__(
        self,
        runtime: DatabaseRuntime,
        writer: WriterQueue,
        artifacts: ArtifactStore,
        settings: OfficialRuntimeSettings,
        clock: TradingClock,
        bar_retention_trading_days: int,
    ) -> None:
        if not settings.official_enabled or settings.threshold_activation == 0:
            raise ValueError("Native TDX worker requires an explicitly enabled official runtime")
        if settings.subject_uid is None:
            raise ValueError("Native TDX worker requires an official subject UID")
        protocol = TdxProtocol()
        self._pools = tuple(TdxServerPool(settings.servers, protocol) for _ in range(5))
        gateways = tuple(TdxLiveClient(pool, protocol) for pool in self._pools)
        self._pipeline = NativeTdxOfficialPipeline(
            runtime,
            writer,
            artifacts,
            gateways[0],
            subject_uid=settings.subject_uid,
            threshold_activation=settings.threshold_activation,
            trading_calendar_file=settings.trading_calendar_file,
            listing_reference_file=settings.listing_reference_file,
            minute_gateways=gateways[1:],
            webhook_url=settings.webhook_url,
            webhook_enabled=settings.webhook_enabled,
        )
        self._orchestrator = OfficialOrchestrator(
            self._pipeline,
            SqliteCycleJournal(runtime, writer),
            OfficialOrchestratorConfig(official_enabled=True),
        )
        self._subject_uid = settings.subject_uid
        self._runtime = runtime
        self._writer = writer
        self._artifacts = artifacts
        self._clock = clock
        self._bar_retention_trading_days = bar_retention_trading_days
        self._last_retention_day: str | None = None

    def __call__(self, cycle_key: str, observed_at: datetime) -> object:
        result = self._orchestrator.run(self._subject_uid, cycle_key, observed_at)
        self._retain_old_minute_bars(observed_at)
        return result

    def _retain_old_minute_bars(self, observed_at: datetime) -> None:
        retention_day = observed_at.astimezone(_CHINA).date().isoformat()
        if retention_day == self._last_retention_day:
            return
        self._last_retention_day = retention_day
        cutoff = _bar_retention_cutoff(
            self._clock,
            observed_at,
            self._bar_retention_trading_days,
        )
        if cutoff is None:
            _logger.info(
                json.dumps(
                    {
                        "event": "tdx_bar_retention_skipped",
                        "reason": "CALENDAR_WINDOW_UNAVAILABLE",
                    },
                    sort_keys=True,
                )
            )
            return
        try:
            plan = plan_tdx_bar_retention(
                self._runtime,
                self._artifacts,
                cutoff,
                now=lambda: datetime.now(UTC),
            )
            apply_tdx_bar_retention(
                self._runtime,
                self._writer,
                self._artifacts,
                plan,
                now=lambda: datetime.now(UTC),
            )
        except Exception as error:  # noqa: BLE001 - cleanup must not kill the live loop
            _logger.error(
                json.dumps(
                    {
                        "event": "tdx_bar_retention_failed",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                    sort_keys=True,
                )
            )

    def close(self) -> None:
        self._orchestrator.close()
        for pool in self._pools:
            pool.close()


def build_worker(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    environ: Mapping[str, str] | None = None,
    *,
    poll_seconds: float = 1.0,
) -> tuple[ContinuousProductionWorker, NativeTdxCycleRunner | None]:
    values = os.environ if environ is None else environ
    settings = OfficialRuntimeSettings.from_environment(
        values,
        data_directory=runtime.paths.data_directory,
    )
    retention_days = _bar_retention_days(
        values.get(
            "MARKET_MONITOR_TDX_BAR_RETENTION_DAYS",
            str(_DEFAULT_BAR_RETENTION_TRADING_DAYS),
        )
    )
    clock = TradingClock(runtime, writer)
    cycle_runner = (
        NativeTdxCycleRunner(
            runtime,
            writer,
            artifacts,
            settings,
            clock,
            retention_days,
        )
        if settings.official_enabled and settings.threshold_activation > 0
        else None
    )
    worker = ContinuousProductionWorker(
        clock,
        cycle_runner,
        poll_seconds=poll_seconds,
    )
    return worker, cycle_runner


def _bar_retention_days(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("MARKET_MONITOR_TDX_BAR_RETENTION_DAYS must be an integer") from error
    if not 1 <= value <= _MAX_BAR_RETENTION_TRADING_DAYS:
        raise ValueError("MARKET_MONITOR_TDX_BAR_RETENTION_DAYS is outside the accepted bounds")
    return value


def _bar_retention_cutoff(
    clock: TradingClock,
    observed_at: datetime,
    keep_days: int,
) -> datetime | None:
    local_date = observed_at.astimezone(_CHINA).date()
    previous = clock.previous_valid_trading_dates(
        "SSE",
        local_date.isoformat(),
        keep_days,
    )
    if len(previous) != keep_days:
        return None
    oldest = min((local_date, *(date.fromisoformat(value) for value in previous)))
    return datetime.combine(oldest, time.min, _CHINA).astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the continuous Market Monitor worker")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    data_directory = arguments.data_dir
    if data_directory is None:
        raw = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not raw:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        data_directory = Path(raw)
    stop = Event()
    runtime_lock = RuntimeLock.acquire(data_directory)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(data_directory))
    writer = WriterQueue(runtime)
    cycle_runner: NativeTdxCycleRunner | None = None
    worker: ContinuousProductionWorker | None = None
    try:
        MigrationManager().upgrade(runtime)
        writer.start()
        artifacts = ArtifactStore(runtime, writer)
        worker, cycle_runner = build_worker(
            runtime,
            writer,
            artifacts,
            poll_seconds=arguments.poll_seconds,
        )
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        worker.start()
        stop.wait()
        return 0
    finally:
        if worker is not None:
            worker.close()
        if cycle_runner is not None:
            cycle_runner.close()
        writer.close()
        runtime.close()
        runtime_lock.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
