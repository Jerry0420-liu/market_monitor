from pathlib import Path

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager


def test_event_notification_schema_and_frozen_registries(tmp_path: Path) -> None:
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
            triggers = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type='trigger'"
                ).scalars()
            )
            statuses = set(
                connection.exec_driver_sql(
                    "SELECT enum_value FROM enum_registry WHERE enum_name='EventStatus'"
                ).scalars()
            )
            generation = connection.exec_driver_sql(
                "SELECT value FROM system_metadata WHERE key='restore_generation'"
            ).scalar_one()
        assert {
            "analysis_commit",
            "audit_record",
            "market_event",
            "event_version",
            "event_evidence",
            "current_event_projection",
            "notification_intent",
            "notification_delivery_state",
            "delivery_attempt",
        } <= tables
        assert {
            "event_version_immutable_update",
            "notification_intent_immutable_update",
            "delivery_attempt_immutable_update",
        } <= triggers
        assert statuses == {"CANDIDATE", "ACTIVE", "RESOLVED", "INVALIDATED"}
        assert generation == "0"
    finally:
        runtime.close()
