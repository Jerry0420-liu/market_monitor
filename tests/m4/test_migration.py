from pathlib import Path

import pytest
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from sqlalchemy.exc import IntegrityError


def test_guardian_schema_and_registries_upgrade(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    try:
        MigrationManager().upgrade(runtime)
        assert MigrationManager().verify(runtime) == "0014_cr003_official_cycle_journal"
        with runtime.read_connection() as connection:
            tables = {
                str(value)
                for value in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).scalars()
            }
            risks = set(
                connection.exec_driver_sql(
                    "SELECT enum_value FROM enum_registry WHERE enum_name='RiskTag'"
                ).scalars()
            )
        assert {"guardian_evaluation", "guardian_risk_tag", "guardian_risk_evidence"} <= tables
        assert risks == {
            "RISING_TOO_FAST",
            "HEAD_CONCENTRATION_HIGH",
            "INTERNAL_DIVERGENCE",
            "CROWDING_INCREASING",
            "LIQUIDITY_WEAKENING",
            "CORE_MEMBERS_WEAKENING",
            "BREADTH_COLLAPSING",
            "STAMPEDE_RISK",
            "T1_CHASING_RISK",
            "DATA_LIMITATION",
            "EARLY_SIGNAL_FAILED",
        }
    finally:
        runtime.close()


def test_blocking_effect_consistency_is_database_enforced(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    try:
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO guardian_evaluation(guardian_uid,state_evaluation_uid,subject_uid,"
                    "snapshot_uid,rule_version,guardian_effect,blocking,max_severity,evaluation_hash,"
                    "created_at) VALUES ('g','missing','missing','missing','1','ALLOW',1,'NONE',?,"
                    "'2026-08-04T00:00:00Z')",
                    ("a" * 64,),
                )
    finally:
        runtime.close()
