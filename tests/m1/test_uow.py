from pathlib import Path

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.uow import optimistic_update
from market_monitor_persistence.writer import TransactionContext, WriterQueue
from sqlalchemy import MetaData, Table


def test_optimistic_update_cannot_overwrite_newer_version(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()

    def seed(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            ("watermark", "first", "2026-08-04T00:00:00Z"),
        )

    def update(transaction: TransactionContext, expected_version: int, value: str) -> bool:
        table = Table("system_metadata", MetaData(), autoload_with=transaction.connection)
        return optimistic_update(
            transaction.connection,
            table,
            table.c.key == "watermark",
            table.c.version,
            expected_version,
            {"value": value, "updated_at": "2026-08-04T00:01:00Z"},
        )

    try:
        writer.submit(seed).result(timeout=10)
        assert writer.submit(lambda transaction: update(transaction, 1, "second")).result(
            timeout=10
        )
        assert not writer.submit(lambda transaction: update(transaction, 1, "stale")).result(
            timeout=10
        )
        with runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT value,version FROM system_metadata WHERE key='watermark'"
            ).one()
        assert tuple(row) == ("second", 2)
    finally:
        writer.close()
        runtime.close()
