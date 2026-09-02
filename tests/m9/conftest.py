from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue

from tests.support.thresholds import activate_test_threshold_pair


@dataclass(frozen=True)
class M9Runtime:
    runtime: DatabaseRuntime
    writer: WriterQueue
    artifacts: ArtifactStore
    data_directory: Path
    referenced_artifacts: frozenset[str]


@pytest.fixture
def m9_runtime(tmp_path: Path) -> Iterator[M9Runtime]:
    data_directory = tmp_path / "source"
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(data_directory))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    artifacts = ArtifactStore(runtime, writer)
    now = datetime(2026, 8, 13, tzinfo=UTC)
    activate_test_threshold_pair(runtime, writer, artifacts, datetime(2026, 8, 4, tzinfo=UTC))
    references = ReferenceRepository(runtime, writer)
    instrument_uid = references.create_instrument("STOCK", now)
    identity_uid = references.add_instrument_identity_version(
        instrument_uid,
        "SSE",
        "600001",
        "M9 Fixture",
        "LISTED",
        "TRADING",
        now,
    )
    references.map_instrument("FIXTURE-M9", "600001", instrument_uid, now)
    references.ensure_analysis_subject("MARKET")
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("FIXTURE-M9", "m9-v1", now)
    CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 1_000, 60)).record(
        epoch_uid, "QUOTES", 1_000_000, 1, now
    )
    ingestion = IngestionService(runtime, writer, artifacts).ingest(
        epoch_uid,
        ProviderBatch(
            "m9-batch-1",
            "2026-08-13T00:00:01Z",
            (ProviderRecord("600001", "2026-08-13T00:00:00Z", "10.0000", 100, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?",
                (ingestion.lineages[0],),
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    snapshots.create_reference_bundle([("INSTRUMENT", instrument_uid, identity_uid)])
    snapshots.create_manifest([quote_uid], datetime(2026, 8, 13, 0, 0, 30, tzinfo=UTC))
    with runtime.read_connection() as connection:
        referenced = frozenset(
            str(value)
            for value in connection.exec_driver_sql(
                "SELECT raw_artifact_sha256 FROM market_data_batch "
                "UNION SELECT artifact_sha256 FROM input_manifest "
                "UNION SELECT report_sha256 FROM threshold_validation "
                "UNION SELECT evidence_sha256 FROM threshold_activation_evidence "
                "UNION SELECT replay_evidence_sha256 FROM threshold_activation_evidence"
            ).scalars()
        )
    try:
        yield M9Runtime(runtime, writer, artifacts, data_directory, referenced)
    finally:
        writer.close()
        runtime.close()
