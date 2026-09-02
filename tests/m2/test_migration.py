from pathlib import Path

import pytest
from alembic import command
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from sqlalchemy.exc import DatabaseError, IntegrityError


def test_existing_m1_database_upgrades_to_m2(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    manager = MigrationManager()
    config = manager._config()
    try:
        with runtime._writer_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_m1_foundation")

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
            "analysis_subject",
            "capability_health_report",
            "capability_watermark",
            "instrument",
            "instrument_identity_version",
            "market_data_batch",
            "market_quote",
            "market_source_epoch",
            "provider_mapping",
            "quote_lineage",
            "raw_market_record",
            "sector",
            "sector_membership",
            "sector_membership_version",
            "sector_version",
            "trading_calendar_day",
            "trading_session",
        } <= tables
    finally:
        runtime.close()


def test_analysis_subject_requires_exact_target_and_unique_identity(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    try:
        with runtime._writer_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO instrument(instrument_uid,instrument_kind,created_at) "
                "VALUES ('instrument-1','STOCK','2026-08-04T00:00:00Z')"
            )
            connection.exec_driver_sql(
                "INSERT INTO analysis_subject"
                "(subject_uid,subject_kind,instrument_uid,sector_uid,created_at) "
                "VALUES ('subject-1','INSTRUMENT','instrument-1',NULL,'2026-08-04T00:00:00Z')"
            )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO analysis_subject"
                    "(subject_uid,subject_kind,instrument_uid,sector_uid,created_at) "
                    "VALUES ('subject-2','INSTRUMENT','instrument-1',NULL,"
                    "'2026-08-04T00:00:00Z')"
                )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO analysis_subject"
                    "(subject_uid,subject_kind,instrument_uid,sector_uid,created_at) "
                    "VALUES ('subject-3','SECTOR',NULL,NULL,'2026-08-04T00:00:00Z')"
                )
    finally:
        runtime.close()


def test_raw_records_are_immutable_and_current_quote_is_unique(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    try:
        with runtime._writer_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO instrument(instrument_uid,instrument_kind,created_at) "
                "VALUES ('instrument-1','STOCK','2026-08-04T00:00:00Z')"
            )
            connection.exec_driver_sql(
                "INSERT INTO market_source_epoch"
                "(epoch_uid,provider_key,capability_fingerprint,started_at,status,"
                "rewarm_required) VALUES ('epoch-1','FIXTURE','v1',"
                "'2026-08-04T00:00:00Z','ACTIVE',0)"
            )
            connection.exec_driver_sql(
                "INSERT INTO artifact_object"
                "(sha256,size_bytes,media_type,relative_path,created_at) VALUES "
                "('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,"
                "'application/json','aa/aa/hash','2026-08-04T00:00:00Z')"
            )
            connection.exec_driver_sql(
                "INSERT INTO market_data_batch"
                "(batch_uid,epoch_uid,provider_batch_id,raw_artifact_sha256,received_at,status) "
                "VALUES ('batch-1','epoch-1','provider-batch-1',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "'2026-08-04T00:00:00Z','RECEIVED')"
            )
            connection.exec_driver_sql(
                "INSERT INTO raw_market_record"
                "(batch_uid,record_index,external_code,raw_source_time,payload_locator,"
                "mapping_status) VALUES ('batch-1',0,'600000','bad-clock','$[0]','RESOLVED')"
            )
            connection.exec_driver_sql(
                "INSERT INTO quote_lineage"
                "(lineage_uid,epoch_uid,instrument_uid,source_time,quote_kind,"
                "business_key_sha256) VALUES ('lineage-1','epoch-1','instrument-1',"
                "'2026-08-04T01:30:00Z','LAST',"
                "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')"
            )
            connection.exec_driver_sql(
                "INSERT INTO market_quote"
                "(quote_uid,lineage_uid,record_version,batch_uid,record_index,received_at,"
                "source_time_raw,source_time,price_scaled,price_scale,price_status,volume,"
                "volume_status,is_current) VALUES ('quote-1','lineage-1',1,'batch-1',0,"
                "'2026-08-04T01:30:01Z','bad-clock',NULL,NULL,4,'INVALID',100,'VALUE',1)"
            )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(DatabaseError, match="immutable"):
                connection.exec_driver_sql(
                    "UPDATE raw_market_record SET external_code='changed' "
                    "WHERE batch_uid='batch-1' AND record_index=0"
                )
        with runtime._writer_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql(
                    "INSERT INTO market_quote"
                    "(quote_uid,lineage_uid,record_version,batch_uid,record_index,received_at,"
                    "source_time_raw,source_time,price_scaled,price_scale,price_status,volume,"
                    "volume_status,is_current) VALUES ('quote-2','lineage-1',2,'batch-1',0,"
                    "'2026-08-04T01:31:01Z','bad-clock',NULL,NULL,4,'INVALID',101,'VALUE',1)"
                )
    finally:
        runtime.close()


def test_m2_tables_reject_non_strict_numeric_values(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    try:
        with runtime._writer_engine.begin() as connection:
            with pytest.raises((IntegrityError, DatabaseError)):
                connection.exec_driver_sql(
                    "INSERT INTO capability_health_report"
                    "(report_uid,epoch_uid,capability,health_status,fitness_status,coverage_ppm,"
                    "latency_ms,observed_at,valid_until) VALUES "
                    "('report-1','missing','QUOTES','HEALTHY','FIT','not-an-integer',0,"
                    "'2026-08-04T00:00:00Z','2026-08-04T00:01:00Z')"
                )
    finally:
        runtime.close()
