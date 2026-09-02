import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from market_monitor_persistence.backup import create_online_backup, verify_backup
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import TransactionContext, WriterQueue


def _runtime(tmp_path: Path) -> tuple[DatabaseRuntime, WriterQueue]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path / "source"))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    return runtime, writer


def _insert(value: int) -> Callable[[TransactionContext], None]:
    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            (f"key-{value}", str(value), "2026-08-04T00:00:00Z"),
        )

    return command


def test_online_backup_is_consistent_at_writer_barrier(tmp_path: Path) -> None:
    runtime, writer = _runtime(tmp_path)
    destination = tmp_path / "backups" / "market-monitor.sqlite3"
    try:
        before = [writer.submit(_insert(value)) for value in range(10)]
        backup = create_online_backup(runtime, writer, destination)
        after = [writer.submit(_insert(value)) for value in range(10, 15)]
        for future in [*before, *after]:
            future.result(timeout=5)

        verification = verify_backup(destination)
        assert verification.ok
        assert verification.integrity_check == "ok"
        assert verification.migration_revision == "0014_cr003_official_cycle_journal"
        assert backup.sha256 == verification.sha256
        with closing(sqlite3.connect(destination)) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM system_metadata WHERE key LIKE 'key-%'"
                ).fetchone()[0]
                == 10
            )
        with runtime.read_connection() as connection:
            count = connection.exec_driver_sql(
                "SELECT count(*) FROM system_metadata WHERE key LIKE 'key-%'"
            ).scalar_one()
            assert count == 15
    finally:
        writer.close()
        runtime.close()


def test_corrupt_backup_fails_verification_without_replacing_source(tmp_path: Path) -> None:
    runtime, writer = _runtime(tmp_path)
    destination = tmp_path / "backups" / "market-monitor.sqlite3"
    try:
        create_online_backup(runtime, writer, destination)
        original_source_hash = verify_backup(runtime.paths.database_file).sha256
        with destination.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"not-sqlite")

        assert not verify_backup(destination).ok
        assert verify_backup(runtime.paths.database_file).sha256 == original_source_hash
    finally:
        writer.close()
        runtime.close()


def test_restart_after_interrupted_write_can_create_valid_backup(tmp_path: Path) -> None:
    runtime, writer = _runtime(tmp_path)
    paths = runtime.paths

    def interrupted(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            ("interrupted", "bad", "2026-08-04T00:00:00Z"),
        )
        raise RuntimeError("simulated process interruption")

    try:
        try:
            writer.submit(interrupted).result(timeout=5)
        except RuntimeError:
            pass
    finally:
        writer.close()
        runtime.close()

    reopened = DatabaseRuntime.open(paths)
    MigrationManager().verify(reopened)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        with reopened.read_connection() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM system_metadata WHERE key='interrupted'"
                ).scalar_one()
                == 0
            )
        destination = tmp_path / "recovered.sqlite3"
        create_online_backup(reopened, reopened_writer, destination)
        assert verify_backup(destination).ok
    finally:
        reopened_writer.close()
        reopened.close()
