from pathlib import Path

import pytest
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from sqlalchemy.exc import DatabaseError, IntegrityError


def test_m3_schema_upgrades_and_contains_required_tables(tmp_path: Path) -> None:
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
        assert {
            "reference_version_bundle",
            "input_manifest",
            "quality_context",
            "evaluation_snapshot",
            "capability_snapshot",
            "rule_execution",
            "fact_record",
            "historical_baseline",
            "state_evaluation",
            "state_transition",
            "current_state_projection",
        } <= tables
    finally:
        runtime.close()


def test_snapshot_state_and_projection_constraints_are_database_enforced(
    tmp_path: Path,
) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    try:
        with runtime._writer_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO analysis_subject"
                "(subject_uid,subject_kind,instrument_uid,sector_uid,created_at) "
                "VALUES ('market','MARKET',NULL,NULL,'2026-08-04T00:00:00Z')"
            )
            connection.exec_driver_sql(
                "INSERT INTO artifact_object"
                "(sha256,size_bytes,media_type,relative_path,created_at) "
                "VALUES (?,1,'application/json','aa/aa/hash','2026-08-04T00:00:00Z')",
                ("a" * 64,),
            )
            connection.exec_driver_sql(
                "INSERT INTO reference_version_bundle(bundle_uid,canonical_hash,created_at) "
                "VALUES ('bundle',?,'2026-08-04T00:00:00Z')",
                ("b" * 64,),
            )
            connection.exec_driver_sql(
                "INSERT INTO input_manifest"
                "(manifest_uid,canonical_hash,artifact_sha256,as_of_time,created_at) "
                "VALUES ('manifest',?,?,'2026-08-04T00:00:00Z','2026-08-04T00:00:00Z')",
                ("c" * 64, "a" * 64),
            )
            connection.exec_driver_sql(
                "INSERT INTO quality_context(quality_context_uid,data_health_status,fitness_status,"
                "evidence_sufficiency,coverage_ppm,max_age_ms,observed_at,valid_until) VALUES "
                "('quality','HEALTHY','FIT','HIGH',1000000,0,'2026-08-04T00:00:00Z',"
                "'2026-08-04T00:01:00Z')"
            )
            connection.exec_driver_sql(
                "INSERT INTO evaluation_snapshot(snapshot_uid,subject_uid,manifest_uid,bundle_uid,"
                "quality_context_uid,as_of_time,max_skew_ms,evaluation_disposition,snapshot_status,"
                "canonical_hash,created_at,sealed_at) VALUES "
                "('snapshot','market','manifest','bundle',"
                "'quality','2026-08-04T00:00:00Z',0,'OFFICIAL','SEALED',?,"
                "'2026-08-04T00:00:00Z','2026-08-04T00:00:00Z')",
                ("d" * 64,),
            )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(DatabaseError, match="immutable"):
                connection.exec_driver_sql(
                    "UPDATE evaluation_snapshot SET max_skew_ms=1 WHERE snapshot_uid='snapshot'"
                )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO state_evaluation(evaluation_uid,snapshot_uid,subject_uid,"
                    "evaluation_disposition,availability_state,lifecycle_state,evaluation_hash,"
                    "created_at) "
                    "VALUES ('bad','snapshot','market','OFFICIAL','WARMING_UP','STARTING',?,"
                    "'2026-08-04T00:00:00Z')",
                    ("e" * 64,),
                )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises((IntegrityError, DatabaseError)):
                connection.exec_driver_sql(
                    "INSERT INTO current_state_projection(subject_uid,availability_state,"
                    "effective_lifecycle_state,last_valid_lifecycle_state,last_valid_as_of_time,"
                    "evaluation_uid,as_of_time,version,rewarm_required) VALUES "
                    "('market','AVAILABLE','OBSERVING',NULL,NULL,'missing',"
                    "'2026-08-04T00:00:00Z','not-an-integer',0)"
                )
    finally:
        runtime.close()
