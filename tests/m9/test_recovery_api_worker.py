from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_api.app import create_app
from market_monitor_api.repository import ApiRepository
from market_monitor_api.security import OwnerSecurity
from market_monitor_notifications.adapters import (
    DeliveryMessage,
    DeliveryResult,
    InAppAdapter,
)
from market_monitor_notifications.recovery import advance_restore_generation
from market_monitor_notifications.worker import DeliveryWorker
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import TransactionContext

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m7.test_security import MutableClock
from tests.m9.conftest import M9Runtime

WORKER_TIME = datetime(2026, 8, 4, 0, 0, 31, tzinfo=UTC)


def _commit_intent(m9_runtime: M9Runtime) -> tuple[str, str]:
    snapshot_uid, subject_uid = _sealed_snapshot(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
    )
    committed = AnalysisCommitService(m9_runtime.runtime, m9_runtime.writer).commit(
        _bound_request(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            snapshot_uid,
        )
    )
    assert len(committed.intent_uids) == 1
    return subject_uid, committed.intent_uids[0]


def _set_recovery_state(m9_runtime: M9Runtime, state: str) -> None:
    updated_at = format_rfc3339(WORKER_TIME)

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) "
            "VALUES ('recovery_state',?,?,1) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value,updated_at=excluded.updated_at,version=system_metadata.version+1",
            (state, updated_at),
        )

    m9_runtime.writer.submit(command).result()


def _client(m9_runtime: M9Runtime, clock: MutableClock) -> TestClient:
    return TestClient(
        create_app(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            OwnerSecurity(m9_runtime.runtime, m9_runtime.writer, clock),
            clock=clock,
        ),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )


def test_delivery_worker_does_not_claim_pending_work_while_recovering(
    m9_runtime: M9Runtime,
) -> None:
    _, intent_uid = _commit_intent(m9_runtime)
    _set_recovery_state(m9_runtime, "RECOVERING")
    adapter = InAppAdapter()
    worker = DeliveryWorker(
        m9_runtime.runtime,
        m9_runtime.writer,
        {"IN_APP": adapter},
        "m9-recovering-worker",
        MutableClock(WORKER_TIME),
    )

    assert worker.run_once() is False

    with m9_runtime.runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count,lease_owner,lease_until "
            "FROM notification_delivery_state WHERE intent_uid=?",
            (intent_uid,),
        ).one()
        attempts = connection.exec_driver_sql(
            "SELECT count(*) FROM delivery_attempt WHERE intent_uid=?",
            (intent_uid,),
        ).scalar_one()
    assert tuple(state) == ("PENDING", 0, None, None)
    assert attempts == 0
    assert adapter.messages == ()


def test_cancelled_old_generation_is_not_sent_after_recovery_returns_normal(
    m9_runtime: M9Runtime,
) -> None:
    _, intent_uid = _commit_intent(m9_runtime)
    suppression = advance_restore_generation(
        m9_runtime.runtime,
        m9_runtime.writer,
        WORKER_TIME,
    )
    _set_recovery_state(m9_runtime, "NORMAL")
    adapter = InAppAdapter()
    worker = DeliveryWorker(
        m9_runtime.runtime,
        m9_runtime.writer,
        {"IN_APP": adapter},
        "m9-post-recovery-worker",
        MutableClock(WORKER_TIME),
    )

    assert suppression.generation == 1
    assert suppression.cancelled == 1
    assert worker.run_once() is False

    with m9_runtime.runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count,last_error_code "
            "FROM notification_delivery_state WHERE intent_uid=?",
            (intent_uid,),
        ).one()
        attempts = connection.exec_driver_sql(
            "SELECT count(*) FROM delivery_attempt WHERE intent_uid=?",
            (intent_uid,),
        ).scalar_one()
    assert tuple(state) == ("CANCELLED", 0, "RESTORE_GENERATION_SUPPRESSED")
    assert attempts == 0
    assert adapter.messages == ()


def test_generation_change_during_adapter_call_keeps_cancellation_without_attempt(
    m9_runtime: M9Runtime,
) -> None:
    _, intent_uid = _commit_intent(m9_runtime)
    _set_recovery_state(m9_runtime, "NORMAL")

    class GenerationAdvancingAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, message: DeliveryMessage) -> DeliveryResult:
            assert message.intent_uid == intent_uid
            self.calls += 1
            advance_restore_generation(
                m9_runtime.runtime,
                m9_runtime.writer,
                WORKER_TIME,
            )
            return DeliveryResult(True, False, "must-not-be-recorded", None)

    adapter = GenerationAdvancingAdapter()
    worker = DeliveryWorker(
        m9_runtime.runtime,
        m9_runtime.writer,
        {"IN_APP": adapter},
        "m9-generation-race-worker",
        MutableClock(WORKER_TIME),
    )

    assert worker.run_once() is True

    with m9_runtime.runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count,lease_owner,lease_until,last_error_code "
            "FROM notification_delivery_state WHERE intent_uid=?",
            (intent_uid,),
        ).one()
        attempts = connection.exec_driver_sql(
            "SELECT count(*) FROM delivery_attempt WHERE intent_uid=?",
            (intent_uid,),
        ).scalar_one()
    assert adapter.calls == 1
    assert tuple(state) == (
        "CANCELLED",
        0,
        None,
        None,
        "RESTORE_GENERATION_SUPPRESSED",
    )
    assert attempts == 0


def test_system_status_exposes_recovering_without_changing_public_shape(
    m9_runtime: M9Runtime,
) -> None:
    _set_recovery_state(m9_runtime, "RECOVERING")
    clock = MutableClock(WORKER_TIME)
    expected = {
        "status": "RECOVERING",
        "database_readable": True,
        "migration_current": True,
        "migration_revision": "0014_cr003_official_cycle_journal",
        "recovery_state": "RECOVERING",
        "restore_generation": 0,
    }

    assert ApiRepository(m9_runtime.runtime, clock=clock).system_status() == expected
    with _client(m9_runtime, clock) as client:
        response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["cache-control"] == "private, no-cache"


def test_readiness_returns_error_view_while_recovering_without_database_write(
    m9_runtime: M9Runtime,
) -> None:
    _set_recovery_state(m9_runtime, "RECOVERING")
    clock = MutableClock(WORKER_TIME)
    with _client(m9_runtime, clock) as client:
        with m9_runtime.runtime.read_connection() as observer:
            before = observer.exec_driver_sql("PRAGMA data_version").scalar_one()
            response = client.get("/health/ready")
            after = observer.exec_driver_sql("PRAGMA data_version").scalar_one()

    assert response.status_code == 503
    assert response.json().keys() == {"code", "message", "request_id"}
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert after == before


@pytest.mark.parametrize(
    ("recovery_state", "rewarm_required"),
    [
        pytest.param("RECOVERING", 0, id="recovery-phase"),
        pytest.param("NORMAL", 1, id="projection-rewarm"),
    ],
)
def test_market_view_maps_recovery_signals_to_warming_up_without_identity_fallback(
    m9_runtime: M9Runtime,
    recovery_state: str,
    rewarm_required: int,
) -> None:
    subject_uid, _ = _commit_intent(m9_runtime)
    baseline = ApiRepository(m9_runtime.runtime).market_view(subject_uid)
    _set_recovery_state(m9_runtime, recovery_state)
    m9_runtime.writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE current_state_projection SET rewarm_required=? WHERE subject_uid=?",
            (rewarm_required, subject_uid),
        )
    ).result()
    repository = ApiRepository(m9_runtime.runtime)

    protected = repository.market_view(subject_uid)

    assert protected.subject_uid == subject_uid
    assert protected.availability_state == "WARMING_UP"
    assert protected.lifecycle_state is None
    assert protected.lifecycle_value_status == "NOT_APPLICABLE"
    assert (protected.confidence.level, protected.confidence.reference_status) == (
        "BLOCKED",
        "NO_JUDGMENT",
    )
    assert protected.last_valid_state == baseline.last_valid_state
    assert protected.last_valid_state.lifecycle_state == "OBSERVING"
    assert protected.last_valid_state.value_status == "VALUE"
    assert (
        protected.guardian.status,
        protected.guardian.effect,
        protected.guardian.blocking,
        protected.guardian.reasons,
    ) == ("BLOCKED", "PAUSE", True, [])
    assert (
        protected.scout.status,
        protected.scout.strength,
        protected.scout.suppressed_by_guardian,
        protected.scout.reasons,
    ) == ("NONE", "LOW", True, [])
    assert protected.as_of_time == baseline.as_of_time
    for key in ("evaluation_uid", "snapshot_uid", "guardian_uid", "scout_uid"):
        assert protected.source[key] == baseline.source[key]

    clock = MutableClock(WORKER_TIME)
    with _client(m9_runtime, clock) as client:
        with m9_runtime.runtime.read_connection() as observer:
            before = observer.exec_driver_sql("PRAGMA data_version").scalar_one()
            response = client.get(f"/api/v1/subjects/{subject_uid}/state")
            after = observer.exec_driver_sql("PRAGMA data_version").scalar_one()
    body = response.json()
    assert response.status_code == 200
    assert body["availability_state"] == "WARMING_UP"
    assert body["lifecycle_state"] is None
    assert body["lifecycle_value_status"] == "NOT_APPLICABLE"
    assert body["confidence"] == {"level": "BLOCKED", "reference_status": "NO_JUDGMENT"}
    assert body["last_valid_state"] == baseline.last_valid_state.model_dump(mode="json")
    assert body["guardian"] == {
        "status": "BLOCKED",
        "effect": "PAUSE",
        "blocking": True,
        "reasons": [],
    }
    assert body["scout"] == {
        "status": "NONE",
        "strength": "LOW",
        "suppressed_by_guardian": True,
        "reasons": [],
    }
    assert after == before


def test_normal_status_readiness_and_market_view_remain_available(
    m9_runtime: M9Runtime,
) -> None:
    subject_uid, _ = _commit_intent(m9_runtime)
    clock = MutableClock(WORKER_TIME)
    repository = ApiRepository(m9_runtime.runtime, clock=clock)

    status = repository.system_status()
    view = repository.market_view(subject_uid)

    assert status["status"] == "READY"
    assert status["recovery_state"] == "NORMAL"
    assert view.availability_state == "AVAILABLE"
    assert view.lifecycle_state == "OBSERVING"
    assert view.lifecycle_value_status == "VALUE"
    assert (view.confidence.level, view.confidence.reference_status) == (
        "HIGH",
        "NORMAL_REFERENCE",
    )
    with _client(m9_runtime, clock) as client:
        ready = client.get("/health/ready")
        state = client.get(f"/api/v1/subjects/{subject_uid}/state")
    assert ready.status_code == 200
    assert ready.json()["status"] == "READY"
    assert ready.json()["recovery_state"] == "NORMAL"
    assert state.status_code == 200
    assert state.json()["availability_state"] == "AVAILABLE"
    assert state.json()["lifecycle_state"] == "OBSERVING"
