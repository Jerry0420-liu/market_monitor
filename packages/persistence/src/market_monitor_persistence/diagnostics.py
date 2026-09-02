from dataclasses import dataclass

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import format_rfc3339, utc_now
from market_monitor_persistence.writer import TransactionContext, WriterQueue


@dataclass(frozen=True)
class DatabaseDiagnostics:
    sqlite_version: str
    journal_mode: str
    synchronous: int
    foreign_keys: int
    auto_vacuum: int
    wal_autocheckpoint: int
    integrity_check: str
    migration_revision: str
    migration_checksum_valid: bool
    artifact_count: int
    expired_lease_count: int


@dataclass(frozen=True)
class CheckpointResult:
    busy: int
    log_frames: int
    checkpointed_frames: int


def collect_database_diagnostics(
    runtime: DatabaseRuntime, artifacts: ArtifactStore
) -> DatabaseDiagnostics:
    revision = MigrationManager().verify(runtime)
    with runtime.read_connection() as connection:
        pragmas = {
            name: connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()
            for name in (
                "journal_mode",
                "synchronous",
                "foreign_keys",
                "auto_vacuum",
                "wal_autocheckpoint",
            )
        }
        integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
        expired = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_lease WHERE valid_until<=?",
                (format_rfc3339(utc_now()),),
            ).scalar_one()
        )
    return DatabaseDiagnostics(
        sqlite_version=runtime.sqlite_version,
        journal_mode=str(pragmas["journal_mode"]),
        synchronous=int(pragmas["synchronous"]),
        foreign_keys=int(pragmas["foreign_keys"]),
        auto_vacuum=int(pragmas["auto_vacuum"]),
        wal_autocheckpoint=int(pragmas["wal_autocheckpoint"]),
        integrity_check=integrity,
        migration_revision=revision,
        migration_checksum_valid=True,
        artifact_count=artifacts.registered_count(),
        expired_lease_count=expired,
    )


def checkpoint(writer: WriterQueue, mode: str = "PASSIVE") -> CheckpointResult:
    normalized = mode.upper()
    if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError("unsupported SQLite checkpoint mode")

    def command(transaction: TransactionContext) -> CheckpointResult:
        row = transaction.connection.exec_driver_sql(f"PRAGMA wal_checkpoint({normalized})").one()
        return CheckpointResult(
            busy=int(row[0]), log_frames=int(row[1]), checkpointed_frames=int(row[2])
        )

    return writer.submit(command).result()
