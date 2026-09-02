from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from market_monitor_data.health import (
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
    WatermarkRepository,
)
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.providers import FixtureReplayProvider
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


def _open(path: Path) -> tuple[DatabaseRuntime, WriterQueue, ArtifactStore]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    return runtime, writer, ArtifactStore(runtime, writer)


def test_checked_in_fixture_covers_required_kinds_and_explicit_gap() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "m2" / "market_replay.json"
    batch = next(iter(FixtureReplayProvider(fixture).batches()))
    assert {record.external_code for record in batch.records} >= {
        "600000",
        "510300",
        "000001",
        "EXPLICIT_GAP",
    }
    gap = next(record for record in batch.records if record.external_code == "EXPLICIT_GAP")
    assert gap.price is None and gap.volume is None
    text = fixture.read_text(encoding="utf-8")
    assert all(kind in text for kind in ("STOCK", "ETF", "INDEX", "INDUSTRY", "CONCEPT"))


def test_mapping_conflict_quarantines_and_artifact_survives_transaction_failure(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m2_runtime
    references = ReferenceRepository(runtime, writer)
    references.record_mapping_conflict(
        "FIXTURE", "BAD", "INSTRUMENT", datetime(2026, 8, 4, tzinfo=UTC)
    )
    instrument = references.create_instrument("INDEX", datetime(2026, 8, 4, tzinfo=UTC))
    references.map_instrument("FIXTURE", "HUGE", instrument, datetime(2026, 8, 4, tzinfo=UTC))
    epoch = SourceEpochService(runtime, writer).start_epoch(
        "FIXTURE", "v1", datetime(2026, 8, 4, tzinfo=UTC)
    )
    service = IngestionService(runtime, writer, artifacts)
    result = service.ingest(
        epoch,
        ProviderBatch(
            "conflict",
            "2026-08-04T01:00:00Z",
            (ProviderRecord("BAD", None, None, None, {}),),
        ),
    )
    assert result.quarantined == 1 and result.quotes == 0
    with pytest.raises(OverflowError):
        service.ingest(
            epoch,
            ProviderBatch(
                "rollback",
                "2026-08-04T01:01:00Z",
                (ProviderRecord("HUGE", "2026-08-04T01:00:00Z", "1e100", 1, {}),),
            ),
        )
    assert len(service.unreferenced_artifacts()) == 1
    with runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM market_data_batch WHERE provider_batch_id='rollback'"
            ).scalar_one()
            == 0
        )


def test_restart_preserves_reference_batch_health_and_rewarming(tmp_path: Path) -> None:
    runtime, writer, artifacts = _open(tmp_path)
    references = ReferenceRepository(runtime, writer)
    sector = references.create_sector("INDUSTRY", datetime(2026, 8, 4, tzinfo=UTC))
    references.add_sector_version(sector, "Industry", datetime(2026, 8, 4, tzinfo=UTC))
    instrument = references.create_instrument("STOCK", datetime(2026, 8, 4, tzinfo=UTC))
    references.map_instrument("FIXTURE", "600000", instrument, datetime(2026, 8, 4, tzinfo=UTC))
    membership = references.freeze_membership(
        sector, "2026-08-04", [instrument], datetime(2026, 8, 4, tzinfo=UTC)
    )
    epochs = SourceEpochService(runtime, writer)
    epoch = epochs.start_epoch("FIXTURE", "v1", datetime(2026, 8, 4, tzinfo=UTC))
    batch = ProviderBatch(
        "restart",
        "2026-08-04T01:00:01Z",
        (ProviderRecord("600000", "2026-08-04T01:00:00Z", "10.0000", 1, {}),),
    )
    first = IngestionService(runtime, writer, artifacts).ingest(epoch, batch)
    health = CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 1000, 30))
    health.record(epoch, "QUOTES", 1_000_000, 1, datetime(2026, 8, 4, tzinfo=UTC))
    WatermarkRepository(runtime, writer).advance(
        epoch,
        "QUOTES",
        datetime(2026, 8, 4, 1, tzinfo=UTC),
        datetime(2026, 8, 4, 1, 0, 1, tzinfo=UTC),
    )
    epochs.start_epoch("FIXTURE", "v2", datetime(2026, 8, 4, 2, tzinfo=UTC))
    writer.close()
    runtime.close()

    runtime, writer, artifacts = _open(tmp_path)
    try:
        assert ReferenceRepository(runtime, writer).membership_as_of(sector, "2026-08-04", 1) == (
            instrument,
        )
        assert membership
        assert IngestionService(runtime, writer, artifacts).ingest(epoch, batch) == first
        assert WatermarkRepository(runtime, writer).get(epoch, "QUOTES").rewarm_required
        assert (
            CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 1000, 30))
            .effective_status(
                epoch, "QUOTES", datetime(2026, 8, 4, tzinfo=UTC) + timedelta(seconds=31)
            )
            .health_status
            == "UNKNOWN"
        )
    finally:
        writer.close()
        runtime.close()
