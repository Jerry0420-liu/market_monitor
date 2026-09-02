from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_notifications.adapters import (
    DeliveryMessage,
    DeliveryResult,
    InAppAdapter,
    WebhookAdapter,
)
from market_monitor_notifications.worker import DeliveryWorker
from sqlalchemy.exc import IntegrityError

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class SequenceAdapter:
    def __init__(self, results: list[DeliveryResult]) -> None:
        self.results = results
        self.messages: list[DeliveryMessage] = []

    def send(self, message: DeliveryMessage) -> DeliveryResult:
        self.messages.append(message)
        return self.results.pop(0)


class CrashAfterEffectAdapter:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def send(self, message: DeliveryMessage) -> DeliveryResult:
        self.keys.append(message.idempotency_key)
        raise SystemExit("simulated process crash after external effect")


def test_worker_delivers_only_after_commit_and_attempt_is_immutable(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, subject = _sealed_snapshot(runtime, writer, artifacts)
    adapter = InAppAdapter()
    assert adapter.messages == ()
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot)
    )
    assert adapter.messages == ()
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 31, tzinfo=UTC))
    worker = DeliveryWorker(runtime, writer, {"IN_APP": adapter}, "worker-a", clock)
    assert worker.run_once()
    assert not worker.run_once()
    assert len(adapter.messages) == 1
    with runtime.read_connection() as connection:
        delivery = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count FROM notification_delivery_state "
            "WHERE intent_uid=?",
            (committed.intent_uids[0],),
        ).one()
        attempts = connection.exec_driver_sql("SELECT count(*) FROM delivery_attempt").scalar_one()
        projection = connection.exec_driver_sql(
            "SELECT version FROM current_state_projection WHERE subject_uid=?", (subject,)
        ).scalar_one()
    assert (delivery.delivery_status, delivery.attempt_count) == ("DELIVERED", 1)
    assert attempts == 1 and projection == 1
    with pytest.raises(IntegrityError, match="delivery_attempt is immutable"):
        writer.submit(
            lambda transaction: transaction.connection.exec_driver_sql(
                "UPDATE delivery_attempt SET outcome='PERMANENT_FAILURE'"
            )
        ).result()


def test_retry_failure_does_not_roll_back_market_truth(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, subject = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot)
    )
    adapter = SequenceAdapter(
        [
            DeliveryResult(False, True, None, "TEMPORARY_CHANNEL_FAILURE"),
            DeliveryResult(True, False, "provider-1", None),
        ]
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 31, tzinfo=UTC))
    worker = DeliveryWorker(
        runtime, writer, {"IN_APP": adapter}, "worker-a", clock, retry_seconds=10
    )
    assert worker.run_once()
    with runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count FROM notification_delivery_state "
            "WHERE intent_uid=?",
            (committed.intent_uids[0],),
        ).one()
        event_count = connection.exec_driver_sql("SELECT count(*) FROM market_event").scalar_one()
        projection = connection.exec_driver_sql(
            "SELECT version FROM current_state_projection WHERE subject_uid=?", (subject,)
        ).scalar_one()
    assert (state.delivery_status, state.attempt_count) == ("RETRY_WAIT", 1)
    assert event_count == 1 and projection == 1
    assert not worker.run_once()
    clock.value += timedelta(seconds=11)
    assert worker.run_once()
    with runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count FROM notification_delivery_state "
            "WHERE intent_uid=?",
            (committed.intent_uids[0],),
        ).one()
    assert (state.delivery_status, state.attempt_count) == ("DELIVERED", 2)
    assert len(adapter.messages) == 2
    assert adapter.messages[0].idempotency_key == adapter.messages[1].idempotency_key


def test_expired_lease_is_recovered_with_same_idempotency_key(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 31, tzinfo=UTC))
    crash = CrashAfterEffectAdapter()
    first = DeliveryWorker(
        runtime, writer, {"IN_APP": crash}, "crashed-worker", clock, lease_seconds=10
    )
    with pytest.raises(SystemExit, match="simulated process crash"):
        first.run_once()
    healthy = InAppAdapter()
    restarted = DeliveryWorker(
        runtime, writer, {"IN_APP": healthy}, "restarted-worker", clock, lease_seconds=10
    )
    assert not restarted.run_once()
    clock.value += timedelta(seconds=11)
    assert restarted.run_once()
    assert crash.keys == [healthy.messages[0].idempotency_key]
    with runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count FROM notification_delivery_state "
            "WHERE intent_uid=?",
            (committed.intent_uids[0],),
        ).one()
    assert (state.delivery_status, state.attempt_count) == ("DELIVERED", 1)


def test_expired_realtime_intent_is_not_delivered(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 6, tzinfo=UTC))
    adapter = InAppAdapter()
    worker = DeliveryWorker(runtime, writer, {"IN_APP": adapter}, "worker-a", clock)
    assert not worker.run_once()
    with runtime.read_connection() as connection:
        status = connection.exec_driver_sql(
            "SELECT delivery_status FROM notification_delivery_state WHERE intent_uid=?",
            (committed.intent_uids[0],),
        ).scalar_one()
    assert status == "EXPIRED" and adapter.messages == ()


def test_retry_is_bounded_and_stops_after_max_attempts(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot)
    )
    failure = DeliveryResult(False, True, None, "TEMPORARY_CHANNEL_FAILURE")
    adapter = SequenceAdapter([failure, failure, failure])
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 31, tzinfo=UTC))
    worker = DeliveryWorker(
        runtime,
        writer,
        {"IN_APP": adapter},
        "worker-a",
        clock,
        retry_seconds=1,
        max_attempts=3,
    )
    for _ in range(3):
        assert worker.run_once()
        clock.value += timedelta(seconds=2)
    assert not worker.run_once()
    with runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count FROM notification_delivery_state "
            "WHERE intent_uid=?",
            (committed.intent_uids[0],),
        ).one()
    assert (state.delivery_status, state.attempt_count) == ("FAILED", 3)
    assert len(adapter.messages) == 3


def test_webhook_is_disabled_by_default_and_sends_idempotency_header_when_enabled() -> None:
    calls: list[tuple[Any, float]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        @property
        def status(self) -> int:
            return 204

    def opener(request: Any, timeout: float) -> Response:
        calls.append((request, timeout))
        return Response()

    message = DeliveryMessage("intent", "key-123", "WEBHOOK", {"event": {"status": "ACTIVE"}})
    disabled = WebhookAdapter("https://example.invalid/hook", opener=opener)
    assert disabled.send(message) == DeliveryResult(False, False, None, "CHANNEL_DISABLED")
    assert calls == []
    enabled = WebhookAdapter("https://example.invalid/hook", enabled=True, opener=opener)
    assert enabled.send(message).delivered
    request, timeout = calls[0]
    assert request.get_header("Idempotency-key") == "key-123"
    assert timeout == 5.0
