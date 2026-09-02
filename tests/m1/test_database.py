from pathlib import Path

import pytest
from market_monitor_persistence.database import (
    DatabaseConfigurationError,
    DatabasePaths,
    DatabaseRuntime,
)
from sqlalchemy.exc import OperationalError


def _pragma(runtime: DatabaseRuntime, name: str) -> object:
    with runtime.read_connection() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()


def test_new_database_enforces_durable_pragmas(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    try:
        assert _pragma(runtime, "journal_mode") == "wal"
        assert _pragma(runtime, "synchronous") == 2
        assert _pragma(runtime, "foreign_keys") == 1
        assert _pragma(runtime, "auto_vacuum") == 2
        assert _pragma(runtime, "wal_autocheckpoint") == 0
        assert _pragma(runtime, "busy_timeout") == 5000
        assert tuple(map(int, runtime.sqlite_version.split("."))) >= (3, 37, 0)
    finally:
        runtime.close()


def test_read_connections_are_query_only(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    try:
        with runtime.read_connection() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            with pytest.raises(OperationalError):
                connection.exec_driver_sql("CREATE TABLE forbidden(value TEXT) STRICT")
    finally:
        runtime.close()


def test_reopen_preserves_wal_configuration(tmp_path: Path) -> None:
    paths = DatabasePaths.from_data_directory(tmp_path)
    first = DatabaseRuntime.open(paths)
    first.close()

    second = DatabaseRuntime.open(paths)
    try:
        assert paths.database_file.is_file()
        assert _pragma(second, "journal_mode") == "wal"
        assert _pragma(second, "auto_vacuum") == 2
    finally:
        second.close()


def test_data_directory_must_be_a_directory(tmp_path: Path) -> None:
    invalid = tmp_path / "not-a-directory"
    invalid.write_text("occupied", encoding="utf-8")

    with pytest.raises(DatabaseConfigurationError, match="directory"):
        DatabaseRuntime.open(DatabasePaths.from_data_directory(invalid))
