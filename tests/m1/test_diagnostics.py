from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.diagnostics import checkpoint, collect_database_diagnostics
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import TransactionContext, WriterQueue


def _runtime(tmp_path: Path) -> tuple[DatabaseRuntime, WriterQueue, ArtifactStore]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    return runtime, writer, ArtifactStore(runtime, writer)


def test_diagnostics_report_durability_migrations_and_artifacts(tmp_path: Path) -> None:
    runtime, writer, artifacts = _runtime(tmp_path)
    past = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        artifact = artifacts.put_bytes(b"diagnostic", "application/octet-stream")
        artifacts.register(artifact)
        artifacts.acquire_lease(
            artifact.sha256,
            "expired-test",
            past + timedelta(minutes=1),
            now=past,
        )

        result = collect_database_diagnostics(runtime, artifacts)

        assert result.sqlite_version == runtime.sqlite_version
        assert result.journal_mode == "wal"
        assert result.synchronous == 2
        assert result.foreign_keys == 1
        assert result.auto_vacuum == 2
        assert result.wal_autocheckpoint == 0
        assert result.integrity_check == "ok"
        assert result.migration_revision == "0014_cr003_official_cycle_journal"
        assert result.migration_checksum_valid
        assert result.artifact_count == 1
        assert result.expired_lease_count == 1
    finally:
        writer.close()
        runtime.close()


def test_checkpoint_waits_behind_active_writer_command(tmp_path: Path) -> None:
    runtime, writer, _ = _runtime(tmp_path)
    entered = Event()
    release = Event()

    def blocking(_: TransactionContext) -> None:
        entered.set()
        assert release.wait(timeout=5)

    try:
        running = writer.submit(blocking)
        assert entered.wait(timeout=5)
        with ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(checkpoint, writer, "PASSIVE")
            assert not pending.done()
            release.set()
            running.result(timeout=5)
            result = pending.result(timeout=5)
        assert result.busy == 0
        assert result.log_frames >= result.checkpointed_frames
    finally:
        release.set()
        writer.close()
        runtime.close()
