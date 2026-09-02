from pathlib import Path

import pytest
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue
from sqlalchemy.exc import IntegrityError


def test_owner_security_and_query_schema_upgrade(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    try:
        MigrationManager().upgrade(runtime)
        assert MigrationManager().verify(runtime) == "0014_cr003_official_cycle_journal"
        with runtime.read_connection() as connection:
            tables = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).scalars()
            )
            setting = connection.exec_driver_sql(
                "SELECT enabled,endpoint_source,version FROM notification_setting WHERE singleton=1"
            ).one()
            triggers = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type='trigger'"
                ).scalars()
            )
            plan = " ".join(
                str(row.detail)
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN SELECT * FROM analysis_query_record "
                    "WHERE owner_uid=? ORDER BY created_at",
                    ("00000000-0000-4000-8000-000000000000",),
                ).all()
            )
            idempotency_columns = {
                str(row.name)
                for row in connection.exec_driver_sql("PRAGMA table_info(api_idempotency)").all()
            }
            query_columns = {
                str(row.name)
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(analysis_query_record)"
                ).all()
            }
        assert {
            "owner_account",
            "owner_session",
            "login_throttle",
            "api_idempotency",
            "analysis_query_record",
            "notification_setting",
        } <= tables
        assert (setting.enabled, setting.endpoint_source, setting.version) == (0, "ENVIRONMENT", 1)
        assert {
            "operation_uid",
            "operation_state",
            "lease_uid",
            "lease_expires_at",
            "attempt_count",
            "updated_at",
            "completed_at",
        } <= idempotency_columns
        assert {"response_json", "updated_at", "completed_at"} <= query_columns
        assert {
            "api_idempotency_state_update",
            "api_idempotency_immutable_delete",
            "analysis_query_record_state_update",
            "analysis_query_record_immutable_delete",
        } <= triggers
        assert "analysis_query_owner_time_idx" in plan
    finally:
        runtime.close()


def test_notification_setting_schema_rejects_inline_secrets_and_extra_rows(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        with pytest.raises(IntegrityError):
            writer.submit(
                lambda transaction: transaction.connection.exec_driver_sql(
                    "UPDATE notification_setting SET endpoint_source='INLINE_SECRET' "
                    "WHERE singleton=1"
                )
            ).result()
        with pytest.raises(IntegrityError):
            writer.submit(
                lambda transaction: transaction.connection.exec_driver_sql(
                    "INSERT INTO notification_setting(singleton,enabled,endpoint_source,"
                    "updated_at,version) VALUES (2,0,'ENVIRONMENT','2026-08-04T00:00:00Z',1)"
                )
            ).result()
    finally:
        writer.close()
        runtime.close()
