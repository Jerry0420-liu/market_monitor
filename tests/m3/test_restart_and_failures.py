from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_analysis.facts import FactExecutor
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_analysis.state import StateService
from market_monitor_data.health import (
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
)
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactIntegrityError, ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


def _build_chain(m3_runtime: tuple[Any, Any, Any]) -> tuple[str, str, str, str]:
    runtime, writer, artifacts = m3_runtime
    now = datetime(2026, 8, 4, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    instrument = references.create_instrument("ETF", now)
    identity = references.add_instrument_identity_version(
        instrument, "SSE", "510300", "Fixture ETF", "LISTED", "TRADING", now
    )
    references.map_instrument("FIXTURE", "510300", instrument, now)
    subject = references.ensure_analysis_subject("INSTRUMENT", instrument)
    epoch = SourceEpochService(runtime, writer).start_epoch("FIXTURE", "restart-v1", now)
    CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 1000, 60)).record(
        epoch, "QUOTES", 1_000_000, 5, now
    )
    ingestion = IngestionService(runtime, writer, artifacts).ingest(
        epoch,
        ProviderBatch(
            "restart-batch",
            "2026-08-04T00:00:01Z",
            (ProviderRecord("510300", "2026-08-04T00:00:00Z", "4.0000", 10, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?", (ingestion.lineages[0],)
            ).scalar_one()
        )
    builder = SnapshotBuilder(runtime, writer, artifacts)
    bundle = builder.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    manifest = builder.create_manifest([quote], datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC))
    snapshot = builder.create_snapshot(
        subject, manifest, bundle, "SHADOW", ["QUOTES"], [], max_skew_ms=1000
    )
    builder.seal(snapshot)
    facts = FactExecutor(runtime, writer).execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")
    assert (
        StateService(runtime, writer)
        .evaluate(snapshot, "AVAILABLE", "OBSERVING", [fact.fact_uid for fact in facts], 0)
        .projection
        is None
    )
    return subject, manifest, snapshot, facts[0].fact_hash


def test_shadow_evaluation_survives_restart_without_official_projection(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    subject, manifest, snapshot, fact_hash = _build_chain(m3_runtime)
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM state_evaluation").scalar_one() == 1
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM current_state_projection").scalar_one()
            == 0
        )

    paths = runtime.paths
    writer.close()
    runtime.close()
    reopened = DatabaseRuntime.open(paths)
    MigrationManager().upgrade(reopened)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        reopened_artifacts = ArtifactStore(reopened, reopened_writer)
        assert SnapshotBuilder(reopened, reopened_writer, reopened_artifacts).replay_manifest(
            manifest
        )["quote_uids"]
        assert (
            FactExecutor(reopened, reopened_writer)
            .execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")[0]
            .fact_hash
            == fact_hash
        )
        with reopened.read_connection() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM state_evaluation").scalar_one()
                == 1
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM current_state_projection"
                ).scalar_one()
                == 0
            )
    finally:
        reopened_writer.close()
        reopened.close()


def test_corrupt_manifest_artifact_is_rejected(m3_runtime: tuple[Any, Any, Any]) -> None:
    runtime, _, artifacts = m3_runtime
    _, manifest, _, _ = _build_chain(m3_runtime)
    with runtime.read_connection() as connection:
        digest = str(
            connection.exec_driver_sql(
                "SELECT artifact_sha256 FROM input_manifest WHERE manifest_uid=?", (manifest,)
            ).scalar_one()
        )
    artifacts.path_for(digest).write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError):
        SnapshotBuilder(runtime, m3_runtime[1], artifacts).replay_manifest(manifest)
