from datetime import UTC, datetime, timedelta
from typing import Any

from market_monitor_data.health import (
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
    WatermarkRepository,
)
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository


def test_ingestion_is_idempotent_preserves_anomaly_and_versions_corrections(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m2_runtime
    references = ReferenceRepository(runtime, writer)
    instrument = references.create_instrument("STOCK", datetime(2026, 8, 4, tzinfo=UTC))
    references.map_instrument("FIXTURE", "600000", instrument, datetime(2026, 8, 4, tzinfo=UTC))
    epoch = SourceEpochService(runtime, writer).start_epoch(
        "FIXTURE", "fixture-v1", datetime(2026, 8, 4, tzinfo=UTC)
    )
    service = IngestionService(runtime, writer, artifacts)
    batch = ProviderBatch(
        "batch-1",
        "2026-08-04T01:31:00Z",
        (ProviderRecord("600000", "bad-clock", "10.2500", 100, {"kept": True}),),
    )
    first = service.ingest(epoch, batch)
    second = service.ingest(epoch, batch)
    assert first == second
    assert first.quotes == 1
    history = service.quote_history(first.lineages[0])
    assert history[0].source_time is None
    assert history[0].source_time_raw == "bad-clock"
    service.correct_quote(
        first.lineages[0], first.batch_uid, 0, "10.2600", 101, "2026-08-04T01:32:00Z"
    )
    assert [quote.record_version for quote in service.quote_history(first.lineages[0])] == [1, 2]


def test_health_expiry_monotonic_watermark_and_epoch_rewarm(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m2_runtime
    epochs = SourceEpochService(runtime, writer)
    first = epochs.start_epoch("FIXTURE", "v1", datetime(2026, 8, 4, tzinfo=UTC))
    health = CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 2_000, 60))
    report = health.record(first, "QUOTES", 850_000, 100, datetime(2026, 8, 4, tzinfo=UTC))
    assert report.health_status == "DEGRADED"
    assert (
        health.effective_status(
            first, "QUOTES", datetime(2026, 8, 4, tzinfo=UTC) + timedelta(seconds=61)
        ).health_status
        == "UNKNOWN"
    )
    watermarks = WatermarkRepository(runtime, writer)
    watermarks.advance(
        first,
        "QUOTES",
        datetime(2026, 8, 4, 1, tzinfo=UTC),
        datetime(2026, 8, 4, 1, 0, 1, tzinfo=UTC),
    )
    unchanged = watermarks.advance(
        first,
        "QUOTES",
        datetime(2026, 8, 4, 0, tzinfo=UTC),
        datetime(2026, 8, 4, 1, 0, 2, tzinfo=UTC),
    )
    assert unchanged.version == 1
    epochs.start_epoch("FIXTURE", "v2", datetime(2026, 8, 4, 2, tzinfo=UTC))
    assert watermarks.get(first, "QUOTES").rewarm_required is True
