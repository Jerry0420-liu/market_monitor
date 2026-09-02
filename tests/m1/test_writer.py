import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import (
    DatabaseBusyError,
    TransactionContext,
    WriterQueue,
    WriterQueueClosedError,
    WriterQueueFullError,
    WriterQueueReentrancyError,
)


def _started_writer(tmp_path: Path) -> tuple[DatabaseRuntime, WriterQueue]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    return runtime, writer


def test_concurrent_callers_share_one_writer_thread(tmp_path: Path) -> None:
    runtime, writer = _started_writer(tmp_path)
    writer_threads: set[int] = set()

    def submit_value(value: int) -> int:
        def command(transaction: TransactionContext) -> int:
            writer_threads.add(get_ident())
            transaction.connection.exec_driver_sql(
                "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
                (f"key-{value}", str(value), "2026-08-04T00:00:00Z"),
            )
            return value

        return writer.submit(command).result(timeout=10)

    try:
        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(submit_value, range(24)))
        assert sorted(results) == list(range(24))
        assert len(writer_threads) == 1
        with runtime.read_connection() as connection:
            count = connection.exec_driver_sql(
                "SELECT count(*) FROM system_metadata WHERE key LIKE 'key-%'"
            ).scalar_one()
            assert count == 24
    finally:
        writer.close()
        runtime.close()


def test_failed_command_rolls_back_and_queue_continues(tmp_path: Path) -> None:
    runtime, writer = _started_writer(tmp_path)

    def failing(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            ("rolled-back", "bad", "2026-08-04T00:00:00Z"),
        )
        raise RuntimeError("injected failure")

    def succeeding(transaction: TransactionContext) -> str:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            ("committed", "good", "2026-08-04T00:00:00Z"),
        )
        return "ok"

    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            writer.submit(failing).result(timeout=10)
        assert writer.submit(succeeding).result(timeout=10) == "ok"
        with runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT key FROM system_metadata WHERE key IN ('committed','rolled-back') "
                "ORDER BY key"
            ).scalars()
            assert list(rows) == ["committed"]
    finally:
        writer.close()
        runtime.close()


def test_nested_submission_is_rejected_without_deadlock(tmp_path: Path) -> None:
    runtime, writer = _started_writer(tmp_path)

    def nested(_: TransactionContext) -> None:
        writer.submit(lambda transaction: None)

    try:
        with pytest.raises(WriterQueueReentrancyError):
            writer.submit(nested).result(timeout=10)
    finally:
        writer.close()
        runtime.close()


def test_close_drains_accepted_work_and_rejects_new_work(tmp_path: Path) -> None:
    runtime, writer = _started_writer(tmp_path)
    futures = []
    for value in range(5):

        def return_value(_: TransactionContext, current: int = value) -> int:
            return current

        futures.append(writer.submit(return_value))

    writer.close()

    assert [future.result(timeout=1) for future in futures] == list(range(5))
    with pytest.raises(WriterQueueClosedError):
        writer.submit(lambda transaction: None)
    runtime.close()


def test_external_lock_is_reported_as_typed_busy_error(tmp_path: Path) -> None:
    runtime, writer = _started_writer(tmp_path)
    rogue = sqlite3.connect(runtime.paths.database_file)
    rogue.execute("PRAGMA busy_timeout=0")
    rogue.execute("BEGIN IMMEDIATE")

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            ("blocked", "value", "2026-08-04T00:00:00Z"),
        )

    try:
        with pytest.raises(DatabaseBusyError):
            writer.submit(command).result(timeout=10)
    finally:
        rogue.rollback()
        rogue.close()
        writer.close()
        runtime.close()


def test_full_queue_times_out_and_cancelled_work_is_not_executed(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime, maxsize=1)
    entered = Event()
    release = Event()
    writer.start()

    def blocking(_: TransactionContext) -> None:
        entered.set()
        assert release.wait(timeout=5)

    def cancelled(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            ("cancelled", "bad", "2026-08-04T00:00:00Z"),
        )

    try:
        running = writer.submit(blocking)
        assert entered.wait(timeout=5)
        pending = writer.submit(cancelled)
        with pytest.raises(WriterQueueFullError):
            writer.submit(lambda _: None, timeout=0)
        assert pending.cancel()
        release.set()
        running.result(timeout=5)
        writer.barrier()
        with runtime.read_connection() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM system_metadata WHERE key='cancelled'"
                ).scalar_one()
                == 0
            )
    finally:
        release.set()
        writer.close()
        runtime.close()
