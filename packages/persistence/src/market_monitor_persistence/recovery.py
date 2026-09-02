from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.backup import verify_backup_set
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.maintenance import reconcile
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import (
    format_rfc3339,
    new_uid,
    sha256_bytes,
    utc_now,
)
from market_monitor_persistence.writer import TransactionContext, WriterQueue

_DATABASE_NAME = "database.sqlite3"
_RUNTIME_DATABASE_NAME = "market-monitor.sqlite3"
_MANIFEST_NAME = "backup-manifest.json"
_MANIFEST_DIGEST_NAME = "backup-manifest.sha256"


class RecoveryError(RuntimeError):
    """Raised when a restore or recovery transition cannot complete safely."""


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    manifest_sha256: str
    restore_generation: int
    cancelled_intents: int
    revoked_sessions: int
    state_projection_count: int
    event_projection_count: int
    watermark_count: int
    started_at: str


@dataclass(frozen=True)
class RecoveryCompletion:
    recovery_state: str
    refreshed_subject_count: int
    refreshed_capability_count: int
    completed_at: str


@dataclass(frozen=True)
class _RecoveryStarted:
    generation: int
    cancelled: int
    revoked: int
    state_projections: int
    event_projections: int
    watermarks: int


def restore_backup_set(
    source: Path,
    destination: Path,
    *,
    now: Callable[[], datetime] = utc_now,
) -> RestoreResult:
    """Install a verified backup into a new data directory in RECOVERING state."""
    source_root = source.resolve()
    target = destination.resolve()
    if target.exists():
        raise RecoveryError("restore destination already exists; a new empty target is required")
    if target == source_root or target.is_relative_to(source_root):
        raise RecoveryError("restore destination must be separate from the backup set")

    verification = verify_backup_set(source_root)
    if not verification.ok:
        raise RecoveryError(f"backup set verification failed: {verification.error_code}")
    required_space = verification.size_bytes + max(16 * 1024 * 1024, verification.size_bytes // 10)
    if shutil.disk_usage(_nearest_existing(target.parent)).free < required_space:
        raise RecoveryError("insufficient free space for restore")

    started_at = format_rfc3339(now())
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{new_uid()}.staging"
    staging.mkdir()
    runtime: DatabaseRuntime | None = None
    writer: WriterQueue | None = None
    published = False
    try:
        _copy_backup_set(source_root, staging)
        copied = verify_backup_set(staging)
        if not copied.ok or copied.manifest_sha256 != verification.manifest_sha256:
            raise RecoveryError(f"copied backup set verification failed: {copied.error_code}")

        os.replace(staging / _DATABASE_NAME, staging / _RUNTIME_DATABASE_NAME)
        (staging / _MANIFEST_NAME).unlink()
        (staging / _MANIFEST_DIGEST_NAME).unlink()
        _sync_tree(staging)

        runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(staging))
        MigrationManager().verify(runtime)
        writer = WriterQueue(runtime)
        writer.start()
        started = writer.submit(
            lambda transaction: _start_recovery(
                transaction,
                started_at,
                verification.manifest_sha256,
            )
        ).result()
        writer.close()
        writer = None
        runtime.close()
        runtime = None
        _sync_all_files(staging)
        _sync_tree(staging)

        try:
            os.rename(staging, target)
        except OSError as error:
            raise RecoveryError("restore publish failed") from error
        _sync_directory(target.parent)
        published = True
        return RestoreResult(
            path=target,
            manifest_sha256=verification.manifest_sha256,
            restore_generation=started.generation,
            cancelled_intents=started.cancelled,
            revoked_sessions=started.revoked,
            state_projection_count=started.state_projections,
            event_projection_count=started.event_projections,
            watermark_count=started.watermarks,
            started_at=started_at,
        )
    except RecoveryError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise RecoveryError("restore failed before publication") from error
    finally:
        if writer is not None:
            writer.close()
        if runtime is not None:
            runtime.close()
        if not published and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def complete_recovery(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    *,
    now: Callable[[], datetime] = utc_now,
) -> RecoveryCompletion:
    """Enter NORMAL only after every restored current subject has fresh evidence."""
    if not reconcile(runtime, artifacts, now=now, allow_rewarm=True).ok:
        raise RecoveryError("recovery reconciliation failed")
    completed_at = format_rfc3339(now())

    def command(transaction: TransactionContext) -> RecoveryCompletion:
        connection = transaction.connection
        metadata = {
            str(row.key): str(row.value)
            for row in connection.exec_driver_sql(
                "SELECT key,value FROM system_metadata WHERE key IN "
                "('recovery_state','recovery_started_at')"
            ).all()
        }
        if metadata.get("recovery_state") != "RECOVERING":
            raise RecoveryError("recovery is not in RECOVERING state")
        started_at = metadata.get("recovery_started_at")
        if started_at is None:
            raise RecoveryError("recovery start metadata is missing")
        if connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() != "ok":
            raise RecoveryError("recovery database integrity check failed")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RecoveryError("recovery foreign-key reconciliation failed")

        capability_issues = int(
            connection.exec_driver_sql(
                "WITH required(epoch_uid,capability) AS ("
                "SELECT epoch_uid,'QUOTES' FROM market_source_epoch WHERE status='ACTIVE' UNION "
                "SELECT DISTINCT cs.epoch_uid,cs.capability FROM current_state_projection p "
                "JOIN state_evaluation e ON e.evaluation_uid=p.evaluation_uid "
                "JOIN capability_snapshot cs ON cs.snapshot_uid=e.snapshot_uid "
                "WHERE cs.required=1) "
                "SELECT count(*) FROM required r LEFT JOIN capability_watermark w "
                "ON w.epoch_uid=r.epoch_uid AND w.capability=r.capability "
                "LEFT JOIN capability_health_report h ON h.report_uid=("
                "SELECT x.report_uid FROM capability_health_report x "
                "WHERE x.epoch_uid=r.epoch_uid AND x.capability=r.capability "
                "AND x.observed_at>? ORDER BY x.observed_at DESC,x.report_uid DESC LIMIT 1) "
                "WHERE w.epoch_uid IS NULL OR w.received_time<=? OR h.report_uid IS NULL "
                "OR h.health_status!='HEALTHY' OR h.fitness_status!='FIT' OR h.valid_until<=?",
                (started_at, started_at, completed_at),
            ).scalar_one()
        )
        if capability_issues:
            raise RecoveryError("fresh healthy watermark rewarm is incomplete")

        subject_issues = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM current_state_projection p "
                "LEFT JOIN analysis_commit c ON c.state_evaluation_uid=p.evaluation_uid "
                "WHERE p.rewarm_required!=0 OR c.commit_uid IS NULL OR c.committed_at<=?",
                (started_at,),
            ).scalar_one()
        )
        if subject_issues:
            raise RecoveryError("fresh OFFICIAL commit recalculation is incomplete")
        invalid_generation_active = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM notification_delivery_state d "
                "JOIN notification_intent i ON i.intent_uid=d.intent_uid "
                "JOIN system_metadata m ON m.key='restore_generation' "
                "WHERE i.restore_generation!=CAST(m.value AS INTEGER) "
                "AND d.delivery_status IN ('PENDING','PROCESSING','RETRY_WAIT')"
            ).scalar_one()
        )
        if invalid_generation_active:
            raise RecoveryError("notification generation reconciliation failed")

        refreshed_subjects = int(
            connection.exec_driver_sql("SELECT count(*) FROM current_state_projection").scalar_one()
        )
        refreshed_capabilities = int(
            connection.exec_driver_sql(
                "WITH required(epoch_uid,capability) AS ("
                "SELECT epoch_uid,'QUOTES' FROM market_source_epoch WHERE status='ACTIVE' UNION "
                "SELECT DISTINCT cs.epoch_uid,cs.capability FROM current_state_projection p "
                "JOIN state_evaluation e ON e.evaluation_uid=p.evaluation_uid "
                "JOIN capability_snapshot cs ON cs.snapshot_uid=e.snapshot_uid "
                "WHERE cs.required=1) SELECT count(*) FROM required"
            ).scalar_one()
        )
        connection.exec_driver_sql(
            "WITH required(epoch_uid,capability) AS ("
            "SELECT epoch_uid,'QUOTES' FROM market_source_epoch WHERE status='ACTIVE' UNION "
            "SELECT DISTINCT cs.epoch_uid,cs.capability FROM current_state_projection p "
            "JOIN state_evaluation e ON e.evaluation_uid=p.evaluation_uid "
            "JOIN capability_snapshot cs ON cs.snapshot_uid=e.snapshot_uid WHERE cs.required=1) "
            "UPDATE capability_watermark SET rewarm_required=0,version=version+1 "
            "WHERE EXISTS (SELECT 1 FROM required r WHERE "
            "r.epoch_uid=capability_watermark.epoch_uid "
            "AND r.capability=capability_watermark.capability)"
        )
        connection.exec_driver_sql(
            "WITH required(epoch_uid) AS ("
            "SELECT epoch_uid FROM market_source_epoch WHERE status='ACTIVE' UNION "
            "SELECT DISTINCT cs.epoch_uid FROM current_state_projection p "
            "JOIN state_evaluation e ON e.evaluation_uid=p.evaluation_uid "
            "JOIN capability_snapshot cs ON cs.snapshot_uid=e.snapshot_uid WHERE cs.required=1) "
            "UPDATE market_source_epoch SET rewarm_required=0 WHERE epoch_uid IN "
            "(SELECT epoch_uid FROM required)"
        )
        connection.exec_driver_sql("UPDATE current_state_projection SET rewarm_required=0")
        _set_metadata(connection, "recovery_state", "NORMAL", completed_at)
        detail_hash = _detail_hash(
            {
                "completed_at": completed_at,
                "refreshed_capabilities": refreshed_capabilities,
                "refreshed_subjects": refreshed_subjects,
            }
        )
        connection.exec_driver_sql(
            "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
            "detail_hash,created_at) VALUES (?,'RECOVERY_COMPLETED',NULL,NULL,?,?)",
            (new_uid(), detail_hash, completed_at),
        )
        return RecoveryCompletion(
            "NORMAL", refreshed_subjects, refreshed_capabilities, completed_at
        )

    return writer.submit(command).result()


def _start_recovery(
    transaction: TransactionContext,
    started_at: str,
    manifest_sha256: str,
) -> _RecoveryStarted:
    connection = transaction.connection
    generation_row = connection.exec_driver_sql(
        "SELECT value,version FROM system_metadata WHERE key='restore_generation'"
    ).one()
    generation = int(generation_row.value) + 1
    connection.exec_driver_sql(
        "UPDATE system_metadata SET value=?,updated_at=?,version=version+1 "
        "WHERE key='restore_generation' AND version=?",
        (str(generation), started_at, generation_row.version),
    )
    cancelled = int(
        connection.exec_driver_sql(
            "UPDATE notification_delivery_state SET delivery_status='CANCELLED',"
            "next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,"
            "last_error_code='RESTORE_GENERATION_SUPPRESSED',version=version+1,updated_at=? "
            "WHERE delivery_status IN ('PENDING','PROCESSING','RETRY_WAIT')",
            (started_at,),
        ).rowcount
    )
    revoked = int(
        connection.exec_driver_sql(
            "UPDATE owner_session SET revoked_at=? WHERE revoked_at IS NULL",
            (started_at,),
        ).rowcount
    )
    _set_metadata(connection, "recovery_state", "RECOVERING", started_at)
    _set_metadata(connection, "recovery_started_at", started_at, started_at)
    _set_metadata(connection, "recovery_manifest_sha256", manifest_sha256, started_at)

    connection.exec_driver_sql("DELETE FROM transition_candidate")
    connection.exec_driver_sql("DELETE FROM current_state_projection")
    state_projections = int(connection.exec_driver_sql(_STATE_REBUILD_SQL).rowcount)
    connection.exec_driver_sql("DELETE FROM current_event_projection")
    event_projections = int(connection.exec_driver_sql(_EVENT_REBUILD_SQL).rowcount)
    connection.exec_driver_sql("DELETE FROM capability_watermark")
    watermarks = int(connection.exec_driver_sql(_WATERMARK_REBUILD_SQL).rowcount)
    connection.exec_driver_sql(
        "UPDATE market_source_epoch SET rewarm_required=1 WHERE status='ACTIVE'"
    )

    generation_hash = _detail_hash({"cancelled": cancelled, "restore_generation": generation})
    connection.exec_driver_sql(
        "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
        "detail_hash,created_at) VALUES (?,'RESTORE_GENERATION_ADVANCED',NULL,NULL,?,?)",
        (new_uid(), generation_hash, started_at),
    )
    recovery_hash = _detail_hash(
        {
            "event_projections": event_projections,
            "manifest_sha256": manifest_sha256,
            "state_projections": state_projections,
            "watermarks": watermarks,
        }
    )
    connection.exec_driver_sql(
        "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
        "detail_hash,created_at) VALUES (?,'RECOVERY_STARTED',NULL,NULL,?,?)",
        (new_uid(), recovery_hash, started_at),
    )
    return _RecoveryStarted(
        generation,
        cancelled,
        revoked,
        state_projections,
        event_projections,
        watermarks,
    )


_STATE_REBUILD_SQL = """
INSERT INTO current_state_projection(
    subject_uid,availability_state,effective_lifecycle_state,
    last_valid_lifecycle_state,last_valid_as_of_time,evaluation_uid,as_of_time,
    version,rewarm_required
)
SELECT
    e.subject_uid,
    e.availability_state,
    e.lifecycle_state,
    (
        SELECT valid.lifecycle_state
        FROM analysis_commit vc
        JOIN state_evaluation valid ON valid.evaluation_uid=vc.state_evaluation_uid
        WHERE valid.subject_uid=e.subject_uid AND valid.availability_state='AVAILABLE'
        ORDER BY vc.committed_at DESC,vc.commit_uid DESC LIMIT 1
    ),
    (
        SELECT vs.as_of_time
        FROM analysis_commit vc
        JOIN state_evaluation valid ON valid.evaluation_uid=vc.state_evaluation_uid
        JOIN evaluation_snapshot vs ON vs.snapshot_uid=valid.snapshot_uid
        WHERE valid.subject_uid=e.subject_uid AND valid.availability_state='AVAILABLE'
        ORDER BY vc.committed_at DESC,vc.commit_uid DESC LIMIT 1
    ),
    e.evaluation_uid,
    s.as_of_time,
    (SELECT count(*) FROM analysis_commit cc JOIN state_evaluation ce
        ON ce.evaluation_uid=cc.state_evaluation_uid WHERE ce.subject_uid=e.subject_uid),
    1
FROM analysis_commit c
JOIN state_evaluation e ON e.evaluation_uid=c.state_evaluation_uid
JOIN evaluation_snapshot s ON s.snapshot_uid=e.snapshot_uid
WHERE c.commit_uid=(
    SELECT latest.commit_uid FROM analysis_commit latest
    JOIN state_evaluation le ON le.evaluation_uid=latest.state_evaluation_uid
    WHERE le.subject_uid=e.subject_uid
    ORDER BY latest.committed_at DESC,latest.commit_uid DESC LIMIT 1
)
"""


_EVENT_REBUILD_SQL = """
INSERT INTO current_event_projection(
    related_key,event_uid,event_version_uid,event_status,version,updated_at
)
SELECT e.related_key,e.event_uid,v.event_version_uid,v.event_status,v.version,v.created_at
FROM market_event e
JOIN event_version v ON v.event_uid=e.event_uid
WHERE v.event_version_uid=(
    SELECT latest.event_version_uid
    FROM event_version latest
    JOIN market_event le ON le.event_uid=latest.event_uid
    WHERE le.related_key=e.related_key
    ORDER BY latest.created_at DESC,latest.version DESC,latest.event_version_uid DESC LIMIT 1
)
"""


_WATERMARK_REBUILD_SQL = """
WITH ranked AS (
    SELECT
        l.epoch_uid,
        q.source_time,
        q.received_at,
        row_number() OVER (
            PARTITION BY l.epoch_uid
            ORDER BY q.source_time DESC,q.received_at DESC,q.record_version DESC,q.quote_uid DESC
        ) AS position
    FROM quote_lineage l
    JOIN market_quote q ON q.lineage_uid=l.lineage_uid
    WHERE q.source_time IS NOT NULL
), versions AS (
    SELECT l.epoch_uid,count(DISTINCT q.source_time) AS version
    FROM quote_lineage l
    JOIN market_quote q ON q.lineage_uid=l.lineage_uid
    WHERE q.source_time IS NOT NULL
    GROUP BY l.epoch_uid
)
INSERT INTO capability_watermark(
    epoch_uid,capability,event_time,received_time,version,rewarm_required
)
SELECT r.epoch_uid,'QUOTES',r.source_time,r.received_at,v.version,
    CASE WHEN e.status='ACTIVE' THEN 1 ELSE 0 END
FROM ranked r
JOIN versions v ON v.epoch_uid=r.epoch_uid
JOIN market_source_epoch e ON e.epoch_uid=r.epoch_uid
WHERE r.position=1
"""


def _set_metadata(connection: Any, key: str, value: str, updated_at: str) -> None:
    connection.exec_driver_sql(
        "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,"
        "version=system_metadata.version+1",
        (key, value, updated_at),
    )


def _copy_backup_set(source: Path, destination: Path) -> None:
    raw_manifest = (source / _MANIFEST_NAME).read_bytes()
    parsed = cast(dict[str, Any], json.loads(raw_manifest))
    database = cast(dict[str, Any], parsed["database"])
    members = [
        str(database["path"]),
        _MANIFEST_NAME,
        _MANIFEST_DIGEST_NAME,
        *(str(item["path"]) for item in cast(list[dict[str, Any]], parsed["artifacts"])),
    ]
    for relative in members:
        source_path = _safe_member(source, relative)
        target_path = _safe_member(destination, relative)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _copy_file_durable(source_path, target_path)
    _sync_tree(destination)


def _copy_file_durable(source: Path, destination: Path) -> None:
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def _sync_all_files(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())


def _sync_tree(root: Path) -> None:
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _sync_directory(directory)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_member(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise RecoveryError("backup set member path is invalid")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RecoveryError("backup set member path is invalid")
    candidate = (root / Path(*parts)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise RecoveryError("backup set member path escapes its root")
    return candidate


def _nearest_existing(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        if candidate.parent == candidate:
            raise RecoveryError("restore destination has no accessible parent")
        candidate = candidate.parent
    return candidate if candidate.is_dir() else candidate.parent


def _detail_hash(value: object) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
