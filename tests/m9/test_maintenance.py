from __future__ import annotations

import importlib
import json
import os
import sqlite3
from concurrent.futures import Future
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Protocol, cast

import pytest
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_api.security import OwnerSecurity
from market_monitor_data.health import WatermarkRepository
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.diagnostics import checkpoint
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import format_rfc3339, parse_rfc3339
from market_monitor_persistence.writer import TransactionContext

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m9.conftest import M9Runtime

NOW = datetime(2026, 8, 13, 0, 0, 30, tzinfo=UTC)


class _ReconciliationReport(Protocol):
    issue_counts: dict[str, int]


def _maintenance() -> ModuleType:
    return importlib.import_module("market_monitor_persistence.maintenance")


def _reconcile(fixture: M9Runtime) -> _ReconciliationReport:
    return cast(
        _ReconciliationReport,
        _maintenance().reconcile(
            fixture.runtime,
            fixture.artifacts,
            now=lambda: NOW,
        ),
    )


def _report_payload(report: _ReconciliationReport) -> dict[str, Any]:
    payload = asdict(cast(Any, report))
    assert set(payload) == {"issue_counts"}
    assert isinstance(payload["issue_counts"], dict)
    assert all(
        isinstance(key, str) and isinstance(value, int) and value >= 0
        for key, value in payload["issue_counts"].items()
    )
    return payload


def _synchronize_watermarks(fixture: M9Runtime) -> None:
    with fixture.runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "SELECT epoch_uid,source_time,received_at FROM ("
            "SELECT l.epoch_uid,q.source_time,q.received_at,"
            "row_number() OVER (PARTITION BY l.epoch_uid ORDER BY q.source_time DESC,"
            "q.received_at DESC,q.record_version DESC,q.quote_uid DESC) AS position "
            "FROM quote_lineage l JOIN market_quote q ON q.lineage_uid=l.lineage_uid "
            "WHERE q.source_time IS NOT NULL) WHERE position=1"
        ).all()
    watermarks = WatermarkRepository(fixture.runtime, fixture.writer)
    for row in rows:
        watermarks.advance(
            str(row.epoch_uid),
            "QUOTES",
            parse_rfc3339(str(row.source_time)),
            parse_rfc3339(str(row.received_at)),
        )


def _commit_official(fixture: M9Runtime) -> tuple[str, str]:
    snapshot_uid, subject_uid = _sealed_snapshot(
        fixture.runtime,
        fixture.writer,
        fixture.artifacts,
    )
    AnalysisCommitService(fixture.runtime, fixture.writer).commit(
        _bound_request(
            fixture.runtime,
            fixture.writer,
            fixture.artifacts,
            snapshot_uid,
        )
    )
    _synchronize_watermarks(fixture)
    with fixture.runtime.read_connection() as connection:
        related_key = str(
            connection.exec_driver_sql(
                "SELECT p.related_key FROM current_event_projection p "
                "JOIN market_event e ON e.event_uid=p.event_uid WHERE e.subject_uid=?",
                (subject_uid,),
            ).scalar_one()
        )
    return subject_uid, related_key


def _put_metadata(fixture: M9Runtime, values: dict[str, str]) -> None:
    updated_at = format_rfc3339(NOW)

    def command(transaction: TransactionContext) -> None:
        for key, value in values.items():
            transaction.connection.exec_driver_sql(
                "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at,version=system_metadata.version+1",
                (key, value, updated_at),
            )

    fixture.writer.submit(command).result()


def test_reconcile_clean_report_is_read_only_and_contains_only_issue_counts(
    m9_runtime: M9Runtime,
) -> None:
    _synchronize_watermarks(m9_runtime)
    database_uri = f"file:{m9_runtime.runtime.paths.database_file.as_posix()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as observer:
        version_before = int(observer.execute("PRAGMA data_version").fetchone()[0])
        report = _reconcile(m9_runtime)
        version_after = int(observer.execute("PRAGMA data_version").fetchone()[0])

    payload = _report_payload(report)
    assert version_after == version_before
    assert sum(report.issue_counts.values()) == 0
    assert payload == {"issue_counts": report.issue_counts}


def test_reconcile_counts_missing_corrupt_and_orphan_artifacts_without_identifiers(
    m9_runtime: M9Runtime,
) -> None:
    missing = m9_runtime.artifacts.put_bytes(b"missing artifact", "application/octet-stream")
    corrupt = m9_runtime.artifacts.put_bytes(b"corrupt artifact", "application/octet-stream")
    orphan = m9_runtime.artifacts.put_bytes(b"orphan artifact", "application/octet-stream")
    m9_runtime.artifacts.register(missing)
    m9_runtime.artifacts.register(corrupt)
    m9_runtime.artifacts.path_for(missing.sha256).unlink()
    m9_runtime.artifacts.path_for(corrupt.sha256).write_bytes(b"tampered")
    orphan_path = m9_runtime.artifacts.path_for(orphan.sha256)
    old_timestamp = (NOW - timedelta(days=1)).timestamp()
    os.utime(orphan_path, (old_timestamp, old_timestamp))

    report = _reconcile(m9_runtime)
    serialized = json.dumps(_report_payload(report), sort_keys=True)

    assert report.issue_counts["artifact_missing"] == 1
    assert report.issue_counts["artifact_corrupt"] == 1
    assert report.issue_counts["artifact_orphan"] == 1
    assert missing.sha256 not in serialized
    assert corrupt.sha256 not in serialized
    assert orphan.sha256 not in serialized
    assert str(m9_runtime.data_directory.resolve()) not in serialized


def test_reconcile_detects_state_and_event_projection_drift_then_clears_after_repair(
    m9_runtime: M9Runtime,
) -> None:
    subject_uid, related_key = _commit_official(m9_runtime)
    with m9_runtime.runtime.read_connection() as connection:
        state_version = int(
            connection.exec_driver_sql(
                "SELECT version FROM current_state_projection WHERE subject_uid=?",
                (subject_uid,),
            ).scalar_one()
        )
        event_version = int(
            connection.exec_driver_sql(
                "SELECT version FROM current_event_projection WHERE related_key=?",
                (related_key,),
            ).scalar_one()
        )

    def drift(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "UPDATE current_state_projection SET version=version+7 WHERE subject_uid=?",
            (subject_uid,),
        )
        transaction.connection.exec_driver_sql(
            "UPDATE current_event_projection SET version=version+7 WHERE related_key=?",
            (related_key,),
        )

    m9_runtime.writer.submit(drift).result()
    drifted = _reconcile(m9_runtime)
    assert drifted.issue_counts["state_projection_drift"] == 1
    assert drifted.issue_counts["event_projection_drift"] == 1

    def repair(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "UPDATE current_state_projection SET version=? WHERE subject_uid=?",
            (state_version, subject_uid),
        )
        transaction.connection.exec_driver_sql(
            "UPDATE current_event_projection SET version=? WHERE related_key=?",
            (event_version, related_key),
        )

    m9_runtime.writer.submit(repair).result()
    repaired = _reconcile(m9_runtime)
    assert repaired.issue_counts["state_projection_drift"] == 0
    assert repaired.issue_counts["event_projection_drift"] == 0


def test_reconcile_detects_active_notification_from_an_old_restore_generation(
    m9_runtime: M9Runtime,
) -> None:
    _commit_official(m9_runtime)
    _put_metadata(
        m9_runtime,
        {
            "restore_generation": "1",
            "recovery_state": "NORMAL",
            "recovery_started_at": format_rfc3339(NOW - timedelta(minutes=1)),
            "recovery_manifest_sha256": "a" * 64,
        },
    )

    report = _reconcile(m9_runtime)

    assert report.issue_counts["notification_generation_drift"] == 1


def test_reconcile_allows_current_generation_notification_while_recovering(
    m9_runtime: M9Runtime,
) -> None:
    _put_metadata(
        m9_runtime,
        {
            "restore_generation": "1",
            "recovery_state": "RECOVERING",
            "recovery_started_at": format_rfc3339(NOW - timedelta(minutes=1)),
            "recovery_manifest_sha256": "a" * 64,
        },
    )
    _commit_official(m9_runtime)

    report = _reconcile(m9_runtime)

    assert report.issue_counts["active_delivery_during_recovery"] == 0


def test_reconcile_detects_incomplete_recovery_metadata(m9_runtime: M9Runtime) -> None:
    _put_metadata(m9_runtime, {"recovery_state": "RECOVERING"})

    report = _reconcile(m9_runtime)

    assert report.issue_counts["recovery_metadata_drift"] > 0


def test_reconcile_detects_active_owner_session_during_recovery(
    m9_runtime: M9Runtime,
) -> None:
    session_time = NOW - timedelta(minutes=2)
    security = OwnerSecurity(m9_runtime.runtime, m9_runtime.writer, lambda: session_time)
    security.bootstrap("m9 maintenance owner password")
    security.login("owner", "m9 maintenance owner password", "127.0.0.1")
    _synchronize_watermarks(m9_runtime)
    _put_metadata(
        m9_runtime,
        {
            "restore_generation": "1",
            "recovery_state": "RECOVERING",
            "recovery_started_at": format_rfc3339(NOW),
            "recovery_manifest_sha256": "b" * 64,
        },
    )
    m9_runtime.writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE market_source_epoch SET rewarm_required=1 WHERE status='ACTIVE'"
        )
    ).result()
    m9_runtime.writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE capability_watermark SET rewarm_required=1"
        )
    ).result()

    report = _reconcile(m9_runtime)

    assert report.issue_counts["active_session_during_recovery"] == 1


def test_reconcile_counts_only_versioned_sectors_missing_canonical_subject_mapping(
    m9_runtime: M9Runtime,
) -> None:
    references = ReferenceRepository(m9_runtime.runtime, m9_runtime.writer)
    mapped_sector_uid = references.create_sector("INDUSTRY", NOW)
    references.add_sector_version(mapped_sector_uid, "Mapped maintenance sector", NOW)
    references.ensure_analysis_subject("SECTOR", mapped_sector_uid)
    missing_sector_uid = references.create_sector("CONCEPT", NOW)
    references.add_sector_version(missing_sector_uid, "Missing maintenance sector", NOW)
    references.create_sector("CONCEPT", NOW)

    report = _reconcile(m9_runtime)
    serialized = json.dumps(_report_payload(report), sort_keys=True)

    assert report.issue_counts["sector_subject_mapping_missing"] == 1
    assert mapped_sector_uid not in serialized
    assert missing_sector_uid not in serialized


def test_truncate_checkpoint_reports_held_reader_busy_then_succeeds_after_release(
    m9_runtime: M9Runtime,
) -> None:
    with m9_runtime.runtime.read_connection() as held_reader:
        held_reader.exec_driver_sql("BEGIN")
        original = int(
            held_reader.exec_driver_sql(
                "SELECT enabled FROM notification_setting WHERE singleton=1"
            ).scalar_one()
        )
        m9_runtime.writer.submit(
            lambda transaction: transaction.connection.exec_driver_sql(
                "UPDATE notification_setting SET enabled=1-enabled,version=version+1 "
                "WHERE singleton=1"
            )
        ).result()

        blocked = checkpoint(m9_runtime.writer, "TRUNCATE")

        assert blocked.busy > 0
        assert (
            held_reader.exec_driver_sql(
                "SELECT enabled FROM notification_setting WHERE singleton=1"
            ).scalar_one()
            == original
        )

    completed = checkpoint(m9_runtime.writer, "TRUNCATE")
    assert completed.busy == 0
    assert completed.log_frames == 0


@pytest.mark.parametrize("pages", [0, -1])
def test_incremental_vacuum_rejects_non_positive_page_count_without_mutation(
    m9_runtime: M9Runtime,
    pages: int,
) -> None:
    with m9_runtime.runtime.read_connection() as connection:
        before = (
            int(connection.exec_driver_sql("PRAGMA page_count").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one()),
        )

    with pytest.raises(ValueError, match="positive"):
        _maintenance().incremental_vacuum(m9_runtime.writer, pages)

    with m9_runtime.runtime.read_connection() as connection:
        after = (
            int(connection.exec_driver_sql("PRAGMA page_count").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one()),
        )
    assert after == before


def test_incremental_vacuum_reports_pages_and_preserves_integrity_and_durability(
    m9_runtime: M9Runtime,
) -> None:
    rows = [
        (
            f"{index:064x}",
            0,
            format_rfc3339(NOW),
            format_rfc3339(NOW),
        )
        for index in range(1, 10_001)
    ]

    def create_free_pages(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO login_throttle(identity_hash,failure_count,window_started_at,"
            "blocked_until,updated_at) VALUES (?,?,?,NULL,?)",
            rows,
        )
        transaction.connection.exec_driver_sql("DELETE FROM login_throttle")

    m9_runtime.writer.submit(create_free_pages).result()
    with m9_runtime.runtime.read_connection() as connection:
        page_count_before = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
        freelist_before = int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one())
        settings_before = (
            str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower(),
            int(connection.exec_driver_sql("PRAGMA synchronous").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA auto_vacuum").scalar_one()),
        )
    revision_before = MigrationManager().verify(m9_runtime.runtime)
    assert freelist_before > 0

    result = _maintenance().incremental_vacuum(m9_runtime.writer, 32)

    with m9_runtime.runtime.read_connection() as connection:
        settings_after = (
            str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower(),
            int(connection.exec_driver_sql("PRAGMA synchronous").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA auto_vacuum").scalar_one()),
        )
        integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
    revision_after = MigrationManager().verify(m9_runtime.runtime)

    assert result.page_count_before == page_count_before
    assert result.freelist_count_before == freelist_before
    assert result.page_count_after < result.page_count_before
    assert result.freelist_count_after < result.freelist_count_before
    assert settings_before == settings_after == ("wal", 2, 1, 2)
    assert integrity == "ok"
    assert revision_after == revision_before == "0014_cr003_official_cycle_journal"


def test_incremental_vacuum_redacts_unwritable_storage_failure() -> None:
    maintenance = _maintenance()

    class FailingWriter:
        def submit(self, _command: Any) -> Future[Any]:
            future: Future[Any] = Future()
            future.set_exception(OSError(r"C:\private\market-monitor.sqlite3 is read-only"))
            return future

    with pytest.raises(
        maintenance.MaintenanceError, match="maintenance operation unavailable"
    ) as raised:
        maintenance.incremental_vacuum(cast(Any, FailingWriter()), 1)

    assert "market-monitor.sqlite3" not in str(raised.value)
    assert "private" not in str(raised.value)
