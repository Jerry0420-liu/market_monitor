from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_analysis.official_orchestrator import SqliteCycleJournal
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import WriterQueue
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 8, 26, 2, 0, tzinfo=UTC)


def test_sqlite_journal_reopens_and_resumes_failed_cycle(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m3_runtime
    subject_uid = ReferenceRepository(runtime, writer).ensure_analysis_subject("MARKET")
    journal = SqliteCycleJournal(runtime, writer)
    started = journal.begin("cycle-reopen", subject_uid, NOW)
    journal.save(
        replace(
            started,
            phase="FAILED",
            status="FAILED",
            error_code="InjectedCrash",
            details={"threshold_activation_count": 2, "metric_evidence": "a" * 64},
        )
    )
    paths = runtime.paths
    writer.close()
    runtime.close()

    reopened = DatabaseRuntime.open(paths)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        assert MigrationManager().verify(reopened) == "0014_cr003_official_cycle_journal"
        resumed_journal = SqliteCycleJournal(reopened, reopened_writer)
        resumed = resumed_journal.begin("cycle-reopen", subject_uid, NOW)

        assert resumed.cycle_uid == started.cycle_uid
        assert resumed.status == "RUNNING"
        assert resumed.attempt == 2
        assert resumed.error_code is None
        assert resumed.details == {
            "threshold_activation_count": 2,
            "metric_evidence": "a" * 64,
        }
    finally:
        reopened_writer.close()
        reopened.close()


def test_sqlite_journal_enforces_subject_and_snapshot_foreign_keys(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m3_runtime
    subject_uid = ReferenceRepository(runtime, writer).ensure_analysis_subject("MARKET")
    journal = SqliteCycleJournal(runtime, writer)

    with pytest.raises(IntegrityError):
        writer.submit(
            lambda transaction: transaction.connection.exec_driver_sql(
                "INSERT INTO official_cycle_run("
                "cycle_uid,cycle_key,subject_uid,observed_at,phase,status,attempt,"
                "detail_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    new_uid(),
                    "unknown-subject",
                    new_uid(),
                    format_rfc3339(NOW),
                    "SNAPSHOT",
                    "RUNNING",
                    1,
                    "{}",
                    format_rfc3339(NOW),
                    format_rfc3339(NOW),
                ),
            )
        ).result()

    started = journal.begin("invalid-snapshot", subject_uid, NOW)
    with pytest.raises(IntegrityError):
        journal.save(replace(started, snapshot_uid=new_uid()))

    assert journal.get("invalid-snapshot", subject_uid) == started
