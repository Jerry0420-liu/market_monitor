from datetime import UTC, datetime
from typing import Any

from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_notifications.recovery import advance_restore_generation

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot


def test_restore_generation_cancels_old_outstanding_realtime_work(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    intent_uids: list[str] = []
    for _ in range(3):
        snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
        commit_result = AnalysisCommitService(runtime, writer).commit(
            _bound_request(runtime, writer, artifacts, snapshot)
        )
        intent_uids.extend(commit_result.intent_uids)
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE notification_delivery_state SET delivery_status='PROCESSING',"
            "lease_owner='old',lease_until='2026-08-04T00:10:00Z' WHERE intent_uid=?",
            (intent_uids[1],),
        )
    ).result()
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE notification_delivery_state SET delivery_status='RETRY_WAIT',"
            "next_attempt_at='2026-08-04T00:10:00Z' WHERE intent_uid=?",
            (intent_uids[2],),
        )
    ).result()
    suppression = advance_restore_generation(
        runtime, writer, datetime(2026, 8, 4, 0, 1, tzinfo=UTC)
    )
    assert suppression.generation == 1 and suppression.cancelled == 3
    with runtime.read_connection() as connection:
        states = set(
            connection.exec_driver_sql(
                "SELECT delivery_status FROM notification_delivery_state"
            ).scalars()
        )
        generation = connection.exec_driver_sql(
            "SELECT value FROM system_metadata WHERE key='restore_generation'"
        ).scalar_one()
        audit = connection.exec_driver_sql(
            "SELECT action FROM audit_record WHERE action='RESTORE_GENERATION_ADVANCED'"
        ).scalar_one()
    assert states == {"CANCELLED"}
    assert generation == "1" and audit == "RESTORE_GENERATION_ADVANCED"
