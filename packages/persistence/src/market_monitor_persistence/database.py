from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool


class DatabaseConfigurationError(ValueError):
    """Raised when the durable database cannot be configured safely."""


@dataclass(frozen=True)
class DatabasePaths:
    data_directory: Path
    database_file: Path
    artifact_directory: Path

    @classmethod
    def from_data_directory(cls, data_directory: Path) -> DatabasePaths:
        return cls(
            data_directory=data_directory,
            database_file=data_directory / "market-monitor.sqlite3",
            artifact_directory=data_directory / "artifacts",
        )


class DatabaseRuntime:
    def __init__(self, paths: DatabasePaths, writer_engine: Engine, reader_engine: Engine) -> None:
        self.paths = paths
        self._writer_engine = writer_engine
        self._reader_engine = reader_engine

    @classmethod
    def open(cls, paths: DatabasePaths) -> DatabaseRuntime:
        cls._prepare_paths(paths)
        cls._bootstrap_sqlite(paths.database_file)
        writer_engine = create_engine(
            f"sqlite+pysqlite:///{paths.database_file.as_posix()}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        event.listen(writer_engine, "connect", cls._configure_writer_connection)
        reader_engine = create_engine(
            f"sqlite+pysqlite:///file:{paths.database_file.as_posix()}?mode=ro&uri=true",
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        event.listen(reader_engine, "connect", cls._configure_reader_connection)
        runtime = cls(paths, writer_engine, reader_engine)
        with runtime.read_connection() as connection:
            connection.exec_driver_sql("SELECT 1")
        return runtime

    @staticmethod
    def _prepare_paths(paths: DatabasePaths) -> None:
        if paths.data_directory.exists() and not paths.data_directory.is_dir():
            raise DatabaseConfigurationError("data path must be a directory")
        try:
            paths.data_directory.mkdir(parents=True, exist_ok=True)
            paths.artifact_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DatabaseConfigurationError("data directory is not writable") from error

    @staticmethod
    def _bootstrap_sqlite(database_file: Path) -> None:
        is_new = not database_file.exists()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database_file)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            if is_new:
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                connection.execute("VACUUM")
            elif connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                raise DatabaseConfigurationError(
                    "existing database must use incremental auto-vacuum"
                )
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise DatabaseConfigurationError("database could not enable WAL mode")
            connection.commit()
        except sqlite3.Error as error:
            raise DatabaseConfigurationError("database could not be initialized safely") from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _configure_common(dbapi_connection: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA wal_autocheckpoint=0")
            cursor.execute("PRAGMA trusted_schema=OFF")
        finally:
            cursor.close()

    @classmethod
    def _configure_writer_connection(cls, dbapi_connection: Any, _: Any) -> None:
        cls._configure_common(dbapi_connection)

    @classmethod
    def _configure_reader_connection(cls, dbapi_connection: Any, _: Any) -> None:
        cls._configure_common(dbapi_connection)
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=ON")
        finally:
            cursor.close()

    @property
    def sqlite_version(self) -> str:
        with self.read_connection() as connection:
            value = connection.exec_driver_sql("SELECT sqlite_version()").scalar_one()
        return str(value)

    @contextmanager
    def read_connection(self) -> Iterator[Connection]:
        with self._reader_engine.connect() as connection:
            yield connection

    def close(self) -> None:
        self._reader_engine.dispose()
        self._writer_engine.dispose()
