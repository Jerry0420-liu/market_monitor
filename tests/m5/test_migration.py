from pathlib import Path

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager


def test_scout_schema_and_frozen_tags_upgrade(tmp_path: Path) -> None:
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
            tags = set(
                connection.exec_driver_sql(
                    "SELECT enum_value FROM enum_registry WHERE enum_name='OpportunityTag'"
                ).scalars()
            )
        assert {"scout_evaluation", "scout_opportunity_tag", "scout_opportunity_evidence"} <= tables
        assert tags == {
            "EARLY_ACTIVITY",
            "HEALTHY_BREADTH",
            "RELATIVE_STRENGTH",
            "TURNOVER_CONFIRMATION",
            "ETF_CONFIRMATION",
            "STYLE_SUPPORT",
            "LOW_CROWDING",
            "CONTINUITY_STRENGTHENING",
        }
    finally:
        runtime.close()
