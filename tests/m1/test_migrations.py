from pathlib import Path

import pytest
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationChecksumError, MigrationManager
from sqlalchemy.exc import DatabaseError, IntegrityError


def _runtime(tmp_path: Path) -> DatabaseRuntime:
    return DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))


def test_empty_database_upgrade_is_repeatable_and_verified(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    manager = MigrationManager()
    try:
        manager.upgrade(runtime)
        manager.upgrade(runtime)

        assert manager.verify(runtime) == "0014_cr003_official_cycle_journal"
        with runtime.read_connection() as connection:
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            assert {
                "alembic_version",
                "artifact_lease",
                "artifact_object",
                "code_registry",
                "enum_registry",
                "schema_migrations",
                "system_metadata",
                "threshold_activation",
                "threshold_entry",
                "threshold_validation",
                "threshold_version",
            } <= tables
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    finally:
        runtime.close()


def test_registry_seeds_are_exact_and_versioned(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        MigrationManager().upgrade(runtime)
        with runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT enum_value, ordinal, source_version FROM enum_registry "
                "WHERE enum_name='ArtifactKind' ORDER BY ordinal"
            ).all()
        assert [tuple(row) for row in rows] == [
            ("RAW_PAYLOAD", 0, "Consolidated Architecture Baseline v1.0"),
            ("INPUT_MANIFEST", 1, "Consolidated Architecture Baseline v1.0"),
            ("BACKUP_MANIFEST", 2, "Consolidated Architecture Baseline v1.0"),
            ("EXPORT", 3, "Consolidated Architecture Baseline v1.0"),
        ]
    finally:
        runtime.close()


def test_strict_foreign_key_unique_check_and_immutable_trigger(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        MigrationManager().upgrade(runtime)
        with runtime._writer_engine.begin() as connection:
            with pytest.raises((IntegrityError, DatabaseError)):
                connection.exec_driver_sql(
                    "INSERT INTO artifact_object "
                    "(sha256,size_bytes,media_type,relative_path,created_at) "
                    "VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                    "'not-an-integer','text/plain','aa/aa/hash','2026-08-04T00:00:00Z')"
                )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO artifact_lease "
                    "(lease_uid,sha256,purpose,valid_until,created_at) VALUES "
                    "('lease-1','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
                    "'backup','2026-08-05T00:00:00Z','2026-08-04T00:00:00Z')"
                )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO enum_registry "
                    "(enum_name,enum_value,ordinal,source_version,active) "
                    "VALUES ('Broken','VALUE',0,'test',2)"
                )
        with runtime._writer_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO artifact_object "
                "(sha256,size_bytes,media_type,relative_path,created_at) VALUES "
                "('cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',"
                "3,'text/plain','cc/cc/hash','2026-08-04T00:00:00Z')"
            )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(DatabaseError, match="immutable"):
                connection.exec_driver_sql(
                    "UPDATE artifact_object SET size_bytes=4 WHERE sha256="
                    "'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'"
                )
    finally:
        runtime.close()


def test_migration_checksum_tampering_is_detected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    manager = MigrationManager()
    try:
        manager.upgrade(runtime)
        with runtime._writer_engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE schema_migrations SET checksum='{'f' * 64}' "
                "WHERE revision='0001_m1_foundation'"
            )

        with pytest.raises(MigrationChecksumError, match="0001_m1_foundation"):
            manager.verify(runtime)
    finally:
        runtime.close()
