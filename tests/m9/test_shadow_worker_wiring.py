from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from market_monitor_analysis.production_worker import ContinuousProductionWorker
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue

from scripts.production_worker import build_worker


class _ClosedClock:
    def phase_at(self, exchange: str, instant: datetime) -> str:
        del exchange, instant
        return "CLOSED"

    def latest_completed_continuous_minute(
        self, exchange: str, instant: datetime
    ) -> datetime | None:
        del exchange, instant
        return None

    def latest_completed_legal_minute(self, exchange: str, instant: datetime) -> datetime | None:
        del exchange, instant
        return None


def test_closed_runtime_constructs_shadow_runner_without_official_side_effects(
    tmp_path: Path,
) -> None:
    data_directory = tmp_path / "data"
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(data_directory))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        worker, cycle_runner = build_worker(
            runtime,
            writer,
            ArtifactStore(runtime, writer),
            environ={
                "MARKET_MONITOR_OFFICIAL_ENABLED": "false",
                "MARKET_MONITOR_THRESHOLD_ACTIVATION": "0",
            },
        )
        try:
            assert cycle_runner.evaluation_disposition == "SHADOW"
            assert worker._run_cycle is not None
            with runtime.read_connection() as connection:
                counts = {
                    table: int(
                        connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
                    )
                    for table in (
                        "analysis_commit",
                        "event_version",
                        "notification_intent",
                        "delivery_attempt",
                        "threshold_activation",
                    )
                }
            assert counts == {table: 0 for table in counts}
        finally:
            worker.close()
            cycle_runner.close()
    finally:
        writer.close()
        runtime.close()


def test_closed_session_worker_does_not_start_a_live_cycle() -> None:
    calls: list[tuple[str, datetime]] = []
    worker = ContinuousProductionWorker(
        _ClosedClock(),
        lambda cycle_key, observed_at: calls.append((cycle_key, observed_at)),
    )

    tick = worker.tick(datetime(2026, 9, 8, 8, 0, tzinfo=UTC))

    assert tick.status == "NO_CYCLE"
    assert calls == []
