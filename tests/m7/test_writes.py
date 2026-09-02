from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from market_monitor_api.security import OwnerSecurity
from market_monitor_api.writes import (
    IdempotencyConflictError,
    VersionConflictError,
    WriteService,
)
from sqlalchemy.exc import IntegrityError

from tests.m7.test_security import MutableClock


def test_notification_settings_are_masked_versioned_idempotent_and_audited(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("settings owner password")
    service = WriteService(runtime, writer, clock, "https://secret.example.test/hook/token")
    initial = service.notification_settings()
    assert initial["enabled"] is False
    assert initial["endpoint_masked"] == "configured"
    assert initial["endpoint_value_status"] == "VALUE"
    assert "secret.example" not in str(initial)
    updated = service.update_notification_settings(
        owner_uid, True, initial["etag"], "settings-key-0001"
    )
    replay = service.update_notification_settings(
        owner_uid, True, initial["etag"], "settings-key-0001"
    )
    assert replay == updated
    assert updated["version"] == 2 and updated["enabled"] is True
    with pytest.raises(IdempotencyConflictError):
        service.update_notification_settings(owner_uid, False, updated["etag"], "settings-key-0001")
    with pytest.raises(VersionConflictError):
        service.update_notification_settings(owner_uid, False, initial["etag"], "settings-key-0002")
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM api_idempotency").scalar_one() == 1
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='NOTIFICATION_SETTINGS_UPDATED'"
            ).scalar_one()
            == 1
        )


def test_webhook_is_disabled_until_environment_configuration_exists(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("disabled owner password")
    service = WriteService(runtime, writer, clock, None)
    initial = service.notification_settings()
    assert initial["endpoint_status"] == "NOT_CONFIGURED"
    assert initial["endpoint_masked"] is None
    assert initial["endpoint_value_status"] == "MISSING"
    with pytest.raises(ValueError, match="not configured"):
        service.update_notification_settings(owner_uid, True, initial["etag"], "settings-key-0003")


def test_completed_settings_update_replays_after_webhook_configuration_disappears(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("replay settings credential")
    configured = WriteService(runtime, writer, clock, "https://example.test/hook")
    initial = configured.notification_settings()
    completed = configured.update_notification_settings(
        owner_uid, True, initial["etag"], "settings-restart-key-0001"
    )

    restarted = WriteService(runtime, writer, clock, None)

    assert (
        restarted.update_notification_settings(
            owner_uid, True, initial["etag"], "settings-restart-key-0001"
        )
        == completed
    )
    with pytest.raises(ValueError, match="not configured"):
        restarted.update_notification_settings(
            owner_uid, True, completed["etag"], "settings-restart-key-0002"
        )


def test_idempotency_claim_serializes_concurrent_duplicate_operation(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("claim owner password")
    service = WriteService(runtime, writer, clock, None)
    assert hasattr(service, "claim"), "durable idempotency claim is required"
    barrier = Barrier(2)

    def attempt() -> Any | None:
        barrier.wait()
        try:
            return service.claim(owner_uid, "CREATE_BACKUP", "concurrent-key-0001", {"v": 1})
        except IdempotencyConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))

    acquired = [item for item in results if item is not None]
    assert len(acquired) == 1
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT operation_state,operation_uid,attempt_count FROM api_idempotency"
        ).one()
    assert row.operation_state == "IN_PROGRESS"
    assert row.operation_uid == acquired[0].operation_uid
    assert row.attempt_count == 1


def test_expired_claim_takeover_keeps_operation_uid_and_completed_response_replays(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("takeover owner password")
    service = WriteService(runtime, writer, clock, None)
    assert hasattr(service, "claim"), "durable idempotency claim is required"
    first = service.claim(owner_uid, "CREATE_BACKUP", "takeover-key-0001", {"v": 1})
    with pytest.raises(IdempotencyConflictError):
        service.claim(owner_uid, "CREATE_BACKUP", "takeover-key-0001", {"v": 1})

    clock.value += timedelta(minutes=10)
    restarted = WriteService(runtime, writer, clock, None)
    takeover = restarted.claim(owner_uid, "CREATE_BACKUP", "takeover-key-0001", {"v": 1})
    assert takeover.operation_uid == first.operation_uid
    assert takeover.lease_uid != first.lease_uid
    response = {"backup_uid": takeover.operation_uid, "verified": True}
    assert restarted.complete(takeover, 201, response, "BACKUP_CREATED") == response
    service.release(first)

    replay = restarted.claim(owner_uid, "CREATE_BACKUP", "takeover-key-0001", {"v": 1})
    assert replay.operation_uid == first.operation_uid
    assert replay.response_status == 201
    assert replay.response == response
    with pytest.raises(IdempotencyConflictError):
        restarted.claim(owner_uid, "CREATE_BACKUP", "takeover-key-0001", {"v": 2})


def test_active_claim_is_not_taken_over_until_the_operation_releases_it(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("active claim owner password")
    service = WriteService(runtime, writer, clock, None)
    first = service.claim(owner_uid, "ANALYSIS_QUERY", "active-key-0001", {"v": 1})

    clock.value += timedelta(minutes=10)
    with pytest.raises(IdempotencyConflictError, match="already in progress"):
        service.claim(owner_uid, "ANALYSIS_QUERY", "active-key-0001", {"v": 1})
    with runtime.read_connection() as connection:
        attempt_count = connection.exec_driver_sql(
            "SELECT attempt_count FROM api_idempotency WHERE operation_uid=?",
            (first.operation_uid,),
        ).scalar_one()
    assert attempt_count == 1

    service.release(first)
    takeover = service.claim(owner_uid, "ANALYSIS_QUERY", "active-key-0001", {"v": 1})
    assert takeover.operation_uid == first.operation_uid
    assert takeover.lease_uid != first.lease_uid


def test_idempotency_completion_and_audit_commit_atomically(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("atomic owner password")
    service = WriteService(runtime, writer, clock, None)
    assert hasattr(service, "claim"), "durable idempotency claim is required"
    claim = service.claim(owner_uid, "CREATE_BACKUP", "atomic-key-0001", {"v": 1})
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "CREATE TRIGGER reject_backup_audit BEFORE INSERT ON audit_record "
            "WHEN NEW.action='BACKUP_CREATED' "
            "BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
    ).result()

    with pytest.raises(IntegrityError, match="injected audit failure"):
        service.complete(claim, 201, {"backup_uid": claim.operation_uid}, "BACKUP_CREATED")

    with runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT operation_state FROM api_idempotency WHERE operation_uid=?",
            (claim.operation_uid,),
        ).scalar_one()
        audits = connection.exec_driver_sql(
            "SELECT count(*) FROM audit_record WHERE action='BACKUP_CREATED'"
        ).scalar_one()
    assert state == "IN_PROGRESS"
    assert audits == 0
