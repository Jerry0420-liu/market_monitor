from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_notifications.adapters import (
    DeliveryAdapter,
    DeliveryMessage,
    DeliveryResult,
)


@dataclass(frozen=True)
class _Claim:
    message: DeliveryMessage
    generation: int


class DeliveryWorker:
    def __init__(
        self,
        runtime: DatabaseRuntime,
        writer: WriterQueue,
        adapters: Mapping[str, DeliveryAdapter],
        worker_id: str,
        clock: Callable[[], datetime],
        *,
        lease_seconds: int = 30,
        retry_seconds: int = 10,
        max_attempts: int = 3,
    ) -> None:
        if not worker_id or min(lease_seconds, retry_seconds, max_attempts) <= 0:
            raise ValueError("worker ID, lease, retry, and max attempts must be positive")
        self._runtime = runtime
        self._writer = writer
        self._adapters = dict(adapters)
        self._worker_id = worker_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._retry_seconds = retry_seconds
        self._max_attempts = max_attempts

    def run_once(self) -> bool:
        now = self._clock()
        now_text = format_rfc3339(now)

        def claim(transaction: TransactionContext) -> _Claim | None:
            connection = transaction.connection
            metadata = {
                str(row.key): str(row.value)
                for row in connection.exec_driver_sql(
                    "SELECT key,value FROM system_metadata WHERE key IN "
                    "('restore_generation','recovery_state')"
                ).all()
            }
            generation = int(metadata.get("restore_generation", "0"))
            if _recovery_state(metadata.get("recovery_state"), generation) != "NORMAL":
                return None
            connection.exec_driver_sql(
                "UPDATE notification_delivery_state SET delivery_status='EXPIRED',"
                "next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,last_error_code='EXPIRED',"
                "version=version+1,updated_at=? WHERE delivery_status IN "
                "('PENDING','PROCESSING','RETRY_WAIT') AND intent_uid IN "
                "(SELECT intent_uid FROM notification_intent WHERE expires_at<=?)",
                (now_text, now_text),
            )
            row = connection.exec_driver_sql(
                "SELECT i.intent_uid,i.idempotency_key,i.channel,i.frozen_context_json,d.version "
                "FROM notification_intent i JOIN notification_delivery_state d "
                "ON d.intent_uid=i.intent_uid WHERE i.restore_generation=? AND i.expires_at>? "
                "AND (i.channel!='WEBHOOK' OR EXISTS (SELECT 1 FROM notification_setting "
                "WHERE singleton=1 AND enabled=1)) AND "
                "((d.delivery_status IN ('PENDING','RETRY_WAIT') AND "
                "(d.next_attempt_at IS NULL OR d.next_attempt_at<=?)) OR "
                "(d.delivery_status='PROCESSING' AND d.lease_until<=?)) "
                "ORDER BY i.created_at,i.intent_uid LIMIT 1",
                (generation, now_text, now_text, now_text),
            ).one_or_none()
            if row is None:
                return None
            lease_until = format_rfc3339(now + timedelta(seconds=self._lease_seconds))
            updated = connection.exec_driver_sql(
                "UPDATE notification_delivery_state SET delivery_status='PROCESSING',"
                "lease_owner=?,lease_until=?,version=version+1,updated_at=? "
                "WHERE intent_uid=? AND version=?",
                (self._worker_id, lease_until, now_text, row.intent_uid, row.version),
            )
            if updated.rowcount != 1:
                return None
            return _Claim(
                DeliveryMessage(
                    str(row.intent_uid),
                    str(row.idempotency_key),
                    str(row.channel),
                    json.loads(str(row.frozen_context_json)),
                ),
                generation,
            )

        claimed = self._writer.submit(claim).result()
        if claimed is None:
            return False
        message = claimed.message
        if not self._claim_is_current(claimed):
            return True
        adapter = self._adapters.get(message.channel)
        if adapter is None:
            outcome = DeliveryResult(False, False, None, "ADAPTER_NOT_CONFIGURED")
        else:
            try:
                outcome = adapter.send(message)
            except Exception:
                outcome = DeliveryResult(False, True, None, "ADAPTER_EXCEPTION")
        completed = self._clock()
        completed_text = format_rfc3339(completed)

        def finish(transaction: TransactionContext) -> bool:
            connection = transaction.connection
            metadata = {
                str(row.key): str(row.value)
                for row in connection.exec_driver_sql(
                    "SELECT key,value FROM system_metadata WHERE key IN "
                    "('restore_generation','recovery_state')"
                ).all()
            }
            generation = int(metadata.get("restore_generation", "0"))
            if (
                _recovery_state(metadata.get("recovery_state"), generation) != "NORMAL"
                or generation != claimed.generation
            ):
                return False
            state = connection.exec_driver_sql(
                "SELECT d.delivery_status,d.attempt_count,d.lease_owner,d.version,"
                "i.restore_generation FROM notification_delivery_state d "
                "JOIN notification_intent i ON i.intent_uid=d.intent_uid WHERE d.intent_uid=?",
                (message.intent_uid,),
            ).one_or_none()
            if (
                state is None
                or int(state.restore_generation) != claimed.generation
                or state.delivery_status != "PROCESSING"
                or state.lease_owner != self._worker_id
            ):
                return False
            attempt = int(state.attempt_count) + 1
            if outcome.delivered:
                status = "DELIVERED"
                attempt_outcome = "DELIVERED"
                next_attempt = None
            elif outcome.retryable and attempt < self._max_attempts:
                status = "RETRY_WAIT"
                attempt_outcome = "RETRYABLE_FAILURE"
                next_attempt = format_rfc3339(completed + timedelta(seconds=self._retry_seconds))
            else:
                status = "FAILED"
                attempt_outcome = "PERMANENT_FAILURE"
                next_attempt = None
            connection.exec_driver_sql(
                "INSERT INTO delivery_attempt(attempt_uid,intent_uid,attempt_number,outcome,"
                "started_at,completed_at,provider_reference,error_code) VALUES (?,?,?,?,?,?,?,?)",
                (
                    new_uid(),
                    message.intent_uid,
                    attempt,
                    attempt_outcome,
                    now_text,
                    completed_text,
                    outcome.provider_reference,
                    outcome.error_code,
                ),
            )
            connection.exec_driver_sql(
                "UPDATE notification_delivery_state SET delivery_status=?,attempt_count=?,"
                "next_attempt_at=?,lease_owner=NULL,lease_until=NULL,last_error_code=?,"
                "version=version+1,updated_at=? WHERE intent_uid=? AND version=?",
                (
                    status,
                    attempt,
                    next_attempt,
                    outcome.error_code,
                    completed_text,
                    message.intent_uid,
                    state.version,
                ),
            )
            return True

        self._writer.submit(finish).result()
        return True

    def _claim_is_current(self, claimed: _Claim) -> bool:
        with self._runtime.read_connection() as connection:
            metadata = {
                str(row.key): str(row.value)
                for row in connection.exec_driver_sql(
                    "SELECT key,value FROM system_metadata WHERE key IN "
                    "('restore_generation','recovery_state')"
                ).all()
            }
            generation = int(metadata.get("restore_generation", "0"))
            state = connection.exec_driver_sql(
                "SELECT d.delivery_status,d.lease_owner,i.restore_generation "
                "FROM notification_delivery_state d JOIN notification_intent i "
                "ON i.intent_uid=d.intent_uid WHERE d.intent_uid=?",
                (claimed.message.intent_uid,),
            ).one_or_none()
        return (
            _recovery_state(metadata.get("recovery_state"), generation) == "NORMAL"
            and generation == claimed.generation
            and state is not None
            and int(state.restore_generation) == claimed.generation
            and state.delivery_status == "PROCESSING"
            and state.lease_owner == self._worker_id
        )


def _recovery_state(value: str | None, generation: int) -> str:
    if value is not None:
        return value
    return "NORMAL" if generation == 0 else "RECOVERING"
