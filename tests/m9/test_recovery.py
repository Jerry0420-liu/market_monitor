from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_api.security import OwnerSecurity
from market_monitor_data.health import (
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
    WatermarkRepository,
)
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.backup import create_backup_set
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m9.conftest import M9Runtime

BACKUP_AT = datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)
RESTORE_AT = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)


@dataclass(frozen=True)
class RecoveryBackup:
    path: Path
    subject_uid: str
    evaluation_uid: str
    related_key: str
    event_uid: str
    event_version_uid: str
    intent_uids: tuple[str, str, str]
    active_epoch_uid: str
    expected_watermarks: tuple[tuple[str, str, str], ...]


def _recovery() -> ModuleType:
    return importlib.import_module("market_monitor_persistence.recovery")


def _next_official_snapshot(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    subject_uid: str,
    second: int,
) -> str:
    with runtime.read_connection() as connection:
        bundle_uid = str(
            connection.exec_driver_sql(
                "SELECT bundle_uid FROM evaluation_snapshot WHERE subject_uid=? "
                "ORDER BY created_at LIMIT 1",
                (subject_uid,),
            ).scalar_one()
        )
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT q.quote_uid FROM market_quote q JOIN quote_lineage l "
                "ON l.lineage_uid=q.lineage_uid JOIN analysis_subject s "
                "ON s.instrument_uid=l.instrument_uid WHERE s.subject_uid=? "
                "ORDER BY q.received_at,q.quote_uid LIMIT 1",
                (subject_uid,),
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    as_of = datetime(2026, 8, 4, 0, 0, second, tzinfo=UTC)
    manifest_uid = snapshots.create_manifest([quote_uid], as_of)
    snapshot_uid = snapshots.create_snapshot(
        subject_uid,
        manifest_uid,
        bundle_uid,
        "OFFICIAL",
        ["QUOTES"],
        [],
        max_skew_ms=1_000,
    )
    snapshots.seal(snapshot_uid)
    return snapshot_uid


@pytest.fixture
def recovery_backup(m9_runtime: M9Runtime, tmp_path: Path) -> RecoveryBackup:
    runtime = m9_runtime.runtime
    writer = m9_runtime.writer
    artifacts = m9_runtime.artifacts
    snapshot, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    commits = AnalysisCommitService(runtime, writer)
    first = commits.commit(_bound_request(runtime, writer, artifacts, snapshot))
    second_snapshot = _next_official_snapshot(runtime, writer, artifacts, subject_uid, 31)
    second = commits.commit(
        replace(
            _bound_request(
                runtime,
                writer,
                artifacts,
                second_snapshot,
                scout_metrics={},
            ),
            expected_projection_version=1,
        )
    )
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE system_metadata SET value='5',version=version+1 WHERE key='restore_generation'"
        )
    ).result()
    third_snapshot = _next_official_snapshot(runtime, writer, artifacts, subject_uid, 32)
    third = commits.commit(
        replace(
            _bound_request(runtime, writer, artifacts, third_snapshot),
            expected_projection_version=2,
        )
    )
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE system_metadata SET value='0',version=version+1 WHERE key='restore_generation'"
        )
    ).result()
    assert len(first.intent_uids) == len(second.intent_uids) == len(third.intent_uids) == 1
    intent_uids = (first.intent_uids[0], second.intent_uids[0], third.intent_uids[0])

    password = "m9 recovery owner password"
    security = OwnerSecurity(runtime, writer, lambda: BACKUP_AT)
    security.bootstrap(password)
    security.login("owner", password, "127.0.0.1")

    with runtime.read_connection() as connection:
        event = connection.exec_driver_sql(
            "SELECT p.related_key,p.event_uid,p.event_version_uid FROM "
            "current_event_projection p JOIN market_event e ON e.event_uid=p.event_uid "
            "WHERE e.subject_uid=?",
            (subject_uid,),
        ).one()
        active_epoch_uid = str(
            connection.exec_driver_sql(
                "SELECT e.epoch_uid FROM analysis_subject s JOIN provider_mapping p "
                "ON p.instrument_uid=s.instrument_uid JOIN market_source_epoch e "
                "ON e.provider_key=p.provider_key WHERE s.subject_uid=? AND e.status='ACTIVE'",
                (subject_uid,),
            ).scalar_one()
        )
        source = connection.exec_driver_sql(
            "SELECT e.provider_key,p.external_code FROM market_source_epoch e "
            "JOIN provider_mapping p ON p.provider_key=e.provider_key "
            "JOIN analysis_subject s ON s.instrument_uid=p.instrument_uid "
            "WHERE e.epoch_uid=? AND s.subject_uid=?",
            (active_epoch_uid, subject_uid),
        ).one()
    IngestionService(runtime, writer, artifacts).ingest(
        active_epoch_uid,
        ProviderBatch(
            "m9-watermark-pair",
            "2026-08-13T00:00:02Z",
            (
                ProviderRecord(
                    str(source.external_code),
                    "2026-08-03T23:59:59Z",
                    "9.9000",
                    99,
                    {},
                ),
            ),
        ),
    )
    with runtime.read_connection() as connection:
        expected_watermarks = tuple(
            (str(row.epoch_uid), str(row.event_time), str(row.received_time))
            for row in connection.exec_driver_sql(
                "SELECT epoch_uid,event_time,received_time FROM ("
                "SELECT l.epoch_uid,q.source_time AS event_time,q.received_at AS received_time,"
                "row_number() OVER (PARTITION BY l.epoch_uid ORDER BY q.source_time DESC,"
                "q.received_at DESC,q.record_version DESC,q.quote_uid DESC) AS position "
                "FROM quote_lineage l JOIN market_quote q ON q.lineage_uid=l.lineage_uid "
                "WHERE q.source_time IS NOT NULL) WHERE position=1 ORDER BY epoch_uid"
            ).all()
        )

    def drift_mutable_state(transaction: TransactionContext) -> None:
        connection = transaction.connection
        connection.exec_driver_sql(
            "UPDATE notification_delivery_state SET delivery_status='PROCESSING',"
            "lease_owner='old-worker',lease_until='2026-08-13T03:00:00Z' "
            "WHERE intent_uid=?",
            (intent_uids[1],),
        )
        connection.exec_driver_sql(
            "UPDATE notification_delivery_state SET delivery_status='RETRY_WAIT',"
            "next_attempt_at='2026-08-13T03:00:00Z' WHERE intent_uid=?",
            (intent_uids[2],),
        )
        connection.exec_driver_sql(
            "INSERT INTO transition_candidate(subject_uid,target_lifecycle_state,"
            "confirmation_count,first_evaluation_uid,latest_evaluation_uid,version) "
            "VALUES (?,'STARTING',1,?,?,1)",
            (subject_uid, third.state_evaluation_uid, third.state_evaluation_uid),
        )
        connection.exec_driver_sql(
            "UPDATE market_source_epoch SET status='RETIRED',ended_at=?,rewarm_required=0 "
            "WHERE status='ACTIVE' AND epoch_uid<>?",
            (format_rfc3339(BACKUP_AT), active_epoch_uid),
        )
        connection.exec_driver_sql("UPDATE market_source_epoch SET rewarm_required=0")
        connection.exec_driver_sql("DELETE FROM capability_watermark")
        connection.exec_driver_sql("DELETE FROM current_event_projection")
        connection.exec_driver_sql("DELETE FROM current_state_projection")

    writer.submit(drift_mutable_state).result()
    backup_path = tmp_path / "verified-backup"
    create_backup_set(
        runtime,
        writer,
        artifacts,
        backup_path,
        now=lambda: BACKUP_AT,
        free_space=lambda _: 10**12,
    )
    return RecoveryBackup(
        backup_path,
        subject_uid,
        third.state_evaluation_uid,
        str(event.related_key),
        str(event.event_uid),
        str(event.event_version_uid),
        intent_uids,
        active_epoch_uid,
        expected_watermarks,
    )


def _restore(backup: RecoveryBackup, destination: Path) -> None:
    _recovery().restore_backup_set(
        backup.path,
        destination,
        now=lambda: RESTORE_AT,
    )


def _open_restored(destination: Path) -> tuple[DatabaseRuntime, WriterQueue, ArtifactStore]:
    runtime = DatabaseRuntime.open(_paths(destination))
    assert MigrationManager().verify(runtime) == "0014_cr003_official_cycle_journal"
    writer = WriterQueue(runtime)
    writer.start()
    return runtime, writer, ArtifactStore(runtime, writer)


def _paths(destination: Path) -> DatabasePaths:
    return DatabasePaths.from_data_directory(destination)


def test_restore_refuses_non_empty_target_without_overwrite(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    destination = tmp_path / "existing-target"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")
    recovery = _recovery()

    with pytest.raises(recovery.RecoveryError, match="empty|exist"):
        recovery.restore_backup_set(
            recovery_backup.path,
            destination,
            now=lambda: RESTORE_AT,
        )

    assert marker.read_text(encoding="utf-8") == "preserve me"
    assert set(destination.iterdir()) == {marker}


def test_restore_refuses_corrupt_set_before_target_creation(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    database = recovery_backup.path / "database.sqlite3"
    database.write_bytes(database.read_bytes() + b"corruption")
    destination = tmp_path / "must-not-exist"
    recovery = _recovery()

    with pytest.raises(recovery.RecoveryError, match="verif|invalid|corrupt"):
        recovery.restore_backup_set(
            recovery_backup.path,
            destination,
            now=lambda: RESTORE_AT,
        )

    assert not destination.exists()


def test_restore_rebuilds_mutable_state_from_immutable_history(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    destination = tmp_path / "restored"
    _restore(recovery_backup, destination)
    runtime, writer, _ = _open_restored(destination)
    try:
        with runtime.read_connection() as connection:
            metadata = {
                str(row.key): str(row.value)
                for row in connection.exec_driver_sql(
                    "SELECT key,value FROM system_metadata WHERE key IN "
                    "('restore_generation','recovery_state','recovery_started_at')"
                ).all()
            }
            deliveries = connection.exec_driver_sql(
                "SELECT intent_uid,delivery_status,lease_owner,lease_until,last_error_code "
                "FROM notification_delivery_state WHERE intent_uid IN (?,?,?) "
                "ORDER BY intent_uid",
                recovery_backup.intent_uids,
            ).all()
            active_sessions = connection.exec_driver_sql(
                "SELECT count(*) FROM owner_session WHERE revoked_at IS NULL"
            ).scalar_one()
            revoked_at = connection.exec_driver_sql(
                "SELECT revoked_at FROM owner_session"
            ).scalar_one()
            state = connection.exec_driver_sql(
                "SELECT availability_state,effective_lifecycle_state,"
                "last_valid_lifecycle_state,last_valid_as_of_time,evaluation_uid,as_of_time,"
                "version,rewarm_required FROM current_state_projection WHERE subject_uid=?",
                (recovery_backup.subject_uid,),
            ).one()
            event = connection.exec_driver_sql(
                "SELECT related_key,event_uid,event_version_uid,event_status,version,updated_at "
                "FROM current_event_projection WHERE related_key=?",
                (recovery_backup.related_key,),
            ).one()
            watermarks = tuple(
                (
                    str(row.epoch_uid),
                    str(row.capability),
                    str(row.event_time),
                    str(row.received_time),
                    int(row.rewarm_required),
                )
                for row in connection.exec_driver_sql(
                    "SELECT epoch_uid,capability,event_time,received_time,rewarm_required "
                    "FROM capability_watermark ORDER BY epoch_uid,capability"
                ).all()
            )
            epoch_rewarm = {
                str(row.status): int(row.rewarm_required)
                for row in connection.exec_driver_sql(
                    "SELECT status,max(rewarm_required) AS rewarm_required "
                    "FROM market_source_epoch GROUP BY status"
                ).all()
            }
            candidates = connection.exec_driver_sql(
                "SELECT count(*) FROM transition_candidate"
            ).scalar_one()
            actions = set(connection.exec_driver_sql("SELECT action FROM audit_record").scalars())

        assert metadata == {
            "restore_generation": "1",
            "recovery_state": "RECOVERING",
            "recovery_started_at": "2026-08-13T02:00:00.000000Z",
        }
        assert {str(row.delivery_status) for row in deliveries} == {"CANCELLED"}
        assert all(row.lease_owner is None and row.lease_until is None for row in deliveries)
        assert {str(row.last_error_code) for row in deliveries} == {"RESTORE_GENERATION_SUPPRESSED"}
        assert active_sessions == 0
        assert revoked_at == "2026-08-13T02:00:00.000000Z"
        assert tuple(state) == (
            "AVAILABLE",
            "OBSERVING",
            "OBSERVING",
            "2026-08-04T00:00:32.000000Z",
            recovery_backup.evaluation_uid,
            "2026-08-04T00:00:32.000000Z",
            3,
            1,
        )
        assert tuple(event) == (
            recovery_backup.related_key,
            recovery_backup.event_uid,
            recovery_backup.event_version_uid,
            "ACTIVE",
            1,
            "2026-08-04T00:00:32.000000Z",
        )
        assert watermarks == tuple(
            (
                epoch_uid,
                "QUOTES",
                event_time,
                received_time,
                int(epoch_uid == recovery_backup.active_epoch_uid),
            )
            for epoch_uid, event_time, received_time in recovery_backup.expected_watermarks
        )
        assert epoch_rewarm["ACTIVE"] == 1
        assert epoch_rewarm.get("RETIRED", 0) == 0
        assert candidates == 0
        assert {"RESTORE_GENERATION_ADVANCED", "RECOVERY_STARTED"} <= actions
    finally:
        writer.close()
        runtime.close()


def test_recovering_phase_survives_process_restart(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    destination = tmp_path / "restored"
    _restore(recovery_backup, destination)
    runtime = DatabaseRuntime.open(_paths(destination))
    runtime.close()

    restarted = DatabaseRuntime.open(_paths(destination))
    try:
        with restarted.read_connection() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT value FROM system_metadata WHERE key='recovery_state'"
                ).scalar_one()
                == "RECOVERING"
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT value FROM system_metadata WHERE key='restore_generation'"
                ).scalar_one()
                == "1"
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM notification_delivery_state "
                    "WHERE delivery_status!='CANCELLED' AND intent_uid IN (?,?,?)",
                    recovery_backup.intent_uids,
                ).scalar_one()
                == 0
            )
    finally:
        restarted.close()


def _fresh_snapshot(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    backup: RecoveryBackup,
) -> str:
    source_time = RESTORE_AT + timedelta(seconds=10)
    received_time = RESTORE_AT + timedelta(seconds=11)
    as_of = RESTORE_AT + timedelta(seconds=12)
    with runtime.read_connection() as connection:
        source = connection.exec_driver_sql(
            "SELECT e.provider_key,p.external_code FROM market_source_epoch e "
            "JOIN provider_mapping p ON p.provider_key=e.provider_key "
            "JOIN analysis_subject s ON s.instrument_uid=p.instrument_uid "
            "WHERE e.epoch_uid=? AND s.subject_uid=?",
            (backup.active_epoch_uid, backup.subject_uid),
        ).one()
        bundle_uid = str(
            connection.exec_driver_sql(
                "SELECT s.bundle_uid FROM analysis_commit c JOIN evaluation_snapshot s "
                "ON s.snapshot_uid=c.snapshot_uid JOIN state_evaluation e "
                "ON e.evaluation_uid=c.state_evaluation_uid WHERE e.subject_uid=? "
                "ORDER BY c.committed_at DESC LIMIT 1",
                (backup.subject_uid,),
            ).scalar_one()
        )

    CapabilityHealthService(
        runtime,
        writer,
        HealthThresholds(900_000, 1_000, 60),
    ).record(backup.active_epoch_uid, "QUOTES", 1_000_000, 1, received_time)
    ingestion = IngestionService(runtime, writer, artifacts).ingest(
        backup.active_epoch_uid,
        ProviderBatch(
            "m9-post-restore",
            format_rfc3339(received_time),
            (
                ProviderRecord(
                    str(source.external_code),
                    format_rfc3339(source_time),
                    "10.1000",
                    101,
                    {},
                ),
            ),
        ),
    )
    WatermarkRepository(runtime, writer).advance(
        backup.active_epoch_uid,
        "QUOTES",
        source_time,
        received_time,
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?",
                (ingestion.lineages[0],),
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    manifest_uid = snapshots.create_manifest([quote_uid], as_of)
    snapshot_uid = snapshots.create_snapshot(
        backup.subject_uid,
        manifest_uid,
        bundle_uid,
        "OFFICIAL",
        ["QUOTES"],
        [],
        max_skew_ms=1_000,
    )
    snapshots.seal(snapshot_uid)
    return snapshot_uid


def test_completion_requires_post_restore_health_and_official_commit_then_audits_normal(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    destination = tmp_path / "restored"
    _restore(recovery_backup, destination)
    runtime, writer, artifacts = _open_restored(destination)
    recovery = _recovery()
    try:
        with pytest.raises(recovery.RecoveryError, match="fresh|rewarm|recovery"):
            recovery.complete_recovery(
                runtime, writer, artifacts, now=lambda: RESTORE_AT + timedelta(seconds=1)
            )

        snapshot_uid = _fresh_snapshot(runtime, writer, artifacts, recovery_backup)
        with pytest.raises(recovery.RecoveryError, match="OFFICIAL|commit|recalculation"):
            recovery.complete_recovery(
                runtime,
                writer,
                artifacts,
                now=lambda: RESTORE_AT + timedelta(seconds=13),
            )

        committed = AnalysisCommitService(runtime, writer).commit(
            replace(
                _bound_request(runtime, writer, artifacts, snapshot_uid),
                expected_projection_version=3,
            )
        )
        recovery.complete_recovery(
            runtime,
            writer,
            artifacts,
            now=lambda: RESTORE_AT + timedelta(seconds=14),
        )
        with runtime.read_connection() as connection:
            state = connection.exec_driver_sql(
                "SELECT value FROM system_metadata WHERE key='recovery_state'"
            ).scalar_one()
            rewarm = connection.exec_driver_sql(
                "SELECT p.rewarm_required,e.rewarm_required,w.rewarm_required "
                "FROM current_state_projection p JOIN market_source_epoch e "
                "ON e.epoch_uid=? JOIN capability_watermark w "
                "ON w.epoch_uid=e.epoch_uid AND w.capability='QUOTES' "
                "WHERE p.subject_uid=?",
                (recovery_backup.active_epoch_uid, recovery_backup.subject_uid),
            ).one()
            completed_audits = connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='RECOVERY_COMPLETED'"
            ).scalar_one()
            old_states = set(
                connection.exec_driver_sql(
                    "SELECT delivery_status FROM notification_delivery_state "
                    "WHERE intent_uid IN (?,?,?)",
                    recovery_backup.intent_uids,
                ).scalars()
            )
            new_states = set(
                connection.exec_driver_sql(
                    "SELECT d.delivery_status FROM notification_delivery_state d "
                    "JOIN notification_intent i ON i.intent_uid=d.intent_uid "
                    "WHERE i.restore_generation=1"
                ).scalars()
            )
        assert state == "NORMAL"
        assert tuple(rewarm) == (0, 0, 0)
        assert completed_audits == 1
        assert old_states == {"CANCELLED"}
        assert new_states == {"PENDING"}
        assert committed.intent_uids
    finally:
        writer.close()
        runtime.close()

    restarted = DatabaseRuntime.open(_paths(destination))
    try:
        with restarted.read_connection() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT value FROM system_metadata WHERE key='recovery_state'"
                ).scalar_one()
                == "NORMAL"
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM audit_record WHERE action='RECOVERY_COMPLETED'"
                ).scalar_one()
                == 1
            )
    finally:
        restarted.close()


def test_completion_refuses_normal_when_reconciliation_detects_a_missing_artifact(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    destination = tmp_path / "restored"
    _restore(recovery_backup, destination)
    runtime, writer, artifacts = _open_restored(destination)
    recovery = _recovery()
    try:
        snapshot_uid = _fresh_snapshot(runtime, writer, artifacts, recovery_backup)
        AnalysisCommitService(runtime, writer).commit(
            replace(
                _bound_request(runtime, writer, artifacts, snapshot_uid),
                expected_projection_version=3,
            )
        )
        with runtime.read_connection() as connection:
            digest = str(
                connection.exec_driver_sql(
                    "SELECT artifact_sha256 FROM input_manifest ORDER BY created_at LIMIT 1"
                ).scalar_one()
            )
        artifacts.path_for(digest).unlink()

        with pytest.raises(recovery.RecoveryError, match="reconciliation"):
            recovery.complete_recovery(
                runtime,
                writer,
                artifacts,
                now=lambda: RESTORE_AT + timedelta(seconds=14),
            )
    finally:
        writer.close()
        runtime.close()


def test_completion_requires_fresh_health_and_watermark_for_unprojected_active_epoch(
    recovery_backup: RecoveryBackup, tmp_path: Path
) -> None:
    destination = tmp_path / "restored"
    _restore(recovery_backup, destination)
    runtime, writer, artifacts = _open_restored(destination)
    recovery = _recovery()
    try:
        auxiliary_epoch_uid = SourceEpochService(runtime, writer).start_epoch(
            "FIXTURE-M9-AUX",
            "m9-aux-v1",
            RESTORE_AT + timedelta(seconds=1),
        )
        snapshot_uid = _fresh_snapshot(runtime, writer, artifacts, recovery_backup)
        AnalysisCommitService(runtime, writer).commit(
            replace(
                _bound_request(runtime, writer, artifacts, snapshot_uid),
                expected_projection_version=3,
            )
        )

        with pytest.raises(recovery.RecoveryError, match="fresh|watermark|rewarm|reconciliation"):
            recovery.complete_recovery(
                runtime,
                writer,
                artifacts,
                now=lambda: RESTORE_AT + timedelta(seconds=14),
            )

        observed_at = RESTORE_AT + timedelta(seconds=15)
        CapabilityHealthService(
            runtime,
            writer,
            HealthThresholds(900_000, 1_000, 60),
        ).record(auxiliary_epoch_uid, "QUOTES", 1_000_000, 1, observed_at)
        WatermarkRepository(runtime, writer).advance(
            auxiliary_epoch_uid,
            "QUOTES",
            observed_at,
            RESTORE_AT + timedelta(seconds=16),
        )
        completed = recovery.complete_recovery(
            runtime,
            writer,
            artifacts,
            now=lambda: RESTORE_AT + timedelta(seconds=17),
        )

        with runtime.read_connection() as connection:
            auxiliary_rewarm = connection.exec_driver_sql(
                "SELECT e.rewarm_required,w.rewarm_required FROM market_source_epoch e "
                "JOIN capability_watermark w ON w.epoch_uid=e.epoch_uid "
                "AND w.capability='QUOTES' WHERE e.epoch_uid=?",
                (auxiliary_epoch_uid,),
            ).one()
        assert completed.recovery_state == "NORMAL"
        assert completed.refreshed_capability_count == 2
        assert tuple(auxiliary_rewarm) == (0, 0)
    finally:
        writer.close()
        runtime.close()
