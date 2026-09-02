from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue


class IdempotencyConflictError(RuntimeError):
    pass


class VersionConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class IdempotencyClaim:
    owner_uid: str
    operation: str
    key_hash: str
    request_hash: str
    operation_uid: str
    lease_uid: str | None
    attempt_count: int
    created_at: str
    response_status: int | None = None
    response: dict[str, Any] | None = None


class WriteService:
    def __init__(
        self,
        runtime: DatabaseRuntime,
        writer: WriterQueue,
        clock: Callable[[], datetime],
        webhook_url: str | None,
    ) -> None:
        self._runtime = runtime
        self._writer = writer
        self._clock = clock
        self._webhook_configured = bool(webhook_url and webhook_url.strip())
        self._active_lock = Lock()
        self._active_claims: dict[tuple[str, str, str], str] = {}

    def notification_settings(self) -> dict[str, Any]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT enabled,endpoint_source,updated_at,version FROM notification_setting "
                "WHERE singleton=1"
            ).one()
        return self._settings_response(row)

    def update_notification_settings(
        self,
        owner_uid: str,
        enabled: bool,
        if_match: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key_hash = canonical_hash(idempotency_key)
        request_hash = canonical_hash({"enabled": enabled, "if_match": if_match})
        now = format_rfc3339(self._clock())
        expires = format_rfc3339(self._clock() + timedelta(hours=24))

        def command(transaction: TransactionContext) -> dict[str, Any]:
            replay = self._idempotent(
                transaction, owner_uid, "UPDATE_NOTIFICATION_SETTINGS", key_hash, request_hash
            )
            if replay is not None:
                return replay
            if enabled and not self._webhook_configured:
                raise ValueError("webhook endpoint is not configured")
            row = transaction.connection.exec_driver_sql(
                "SELECT enabled,endpoint_source,updated_at,version FROM notification_setting "
                "WHERE singleton=1"
            ).one()
            expected = f'"notification-settings-v{int(row.version)}"'
            if if_match != expected:
                raise VersionConflictError("notification settings version changed")
            transaction.connection.exec_driver_sql(
                "UPDATE notification_setting SET enabled=?,updated_at=?,version=version+1 "
                "WHERE singleton=1 AND version=?",
                (int(enabled), now, int(row.version)),
            )
            updated = transaction.connection.exec_driver_sql(
                "SELECT enabled,endpoint_source,updated_at,version FROM notification_setting "
                "WHERE singleton=1"
            ).one()
            response = self._settings_response(updated)
            response_json = json.dumps(response, sort_keys=True, separators=(",", ":"))
            transaction.connection.exec_driver_sql(
                "INSERT INTO api_idempotency(owner_uid,operation,key_hash,request_hash,"
                "operation_uid,operation_state,lease_uid,lease_expires_at,attempt_count,"
                "response_status,response_json,created_at,updated_at,completed_at,expires_at) "
                "VALUES (?,?,?,?,?,'COMPLETED',NULL,NULL,1,200,?,?,?,?,?)",
                (
                    owner_uid,
                    "UPDATE_NOTIFICATION_SETTINGS",
                    key_hash,
                    request_hash,
                    new_uid(),
                    response_json,
                    now,
                    now,
                    now,
                    expires,
                ),
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
                "detail_hash,created_at) VALUES (?, 'NOTIFICATION_SETTINGS_UPDATED',NULL,NULL,?,?)",
                (new_uid(), canonical_hash(response), now),
            )
            return response

        return self._writer.submit(command).result()

    def claim(
        self,
        owner_uid: str,
        operation: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> IdempotencyClaim:
        key_hash = canonical_hash(idempotency_key)
        request_hash = canonical_hash(request)
        now_value = self._clock()
        now = format_rfc3339(now_value)
        lease_expires = format_rfc3339(now_value + timedelta(minutes=5))
        expires = format_rfc3339(now_value + timedelta(hours=24))
        operation_uid = new_uid()
        lease_uid = new_uid()
        active_key = (owner_uid, operation, key_hash)

        with self._active_lock:
            if active_key in self._active_claims:
                raise IdempotencyConflictError("idempotent operation is already in progress")
            self._active_claims[active_key] = lease_uid

        def command(transaction: TransactionContext) -> IdempotencyClaim:
            row = transaction.connection.exec_driver_sql(
                "SELECT request_hash,operation_uid,operation_state,lease_uid,lease_expires_at,"
                "attempt_count,created_at,response_status,response_json FROM api_idempotency "
                "WHERE owner_uid=? AND operation=? AND key_hash=?",
                (owner_uid, operation, key_hash),
            ).one_or_none()
            if row is None:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO api_idempotency(owner_uid,operation,key_hash,request_hash,"
                    "operation_uid,operation_state,lease_uid,lease_expires_at,attempt_count,"
                    "response_status,response_json,created_at,updated_at,completed_at,expires_at) "
                    "VALUES (?,?,?,?,?,'IN_PROGRESS',?,?,1,NULL,NULL,?,?,NULL,?)",
                    (
                        owner_uid,
                        operation,
                        key_hash,
                        request_hash,
                        operation_uid,
                        lease_uid,
                        lease_expires,
                        now,
                        now,
                        expires,
                    ),
                )
                return IdempotencyClaim(
                    owner_uid,
                    operation,
                    key_hash,
                    request_hash,
                    operation_uid,
                    lease_uid,
                    1,
                    now,
                )
            if row.request_hash != request_hash:
                raise IdempotencyConflictError("idempotency key was used for another request")
            if row.operation_state == "COMPLETED":
                return IdempotencyClaim(
                    owner_uid,
                    operation,
                    key_hash,
                    request_hash,
                    str(row.operation_uid),
                    None if row.lease_uid is None else str(row.lease_uid),
                    int(row.attempt_count),
                    str(row.created_at),
                    int(row.response_status),
                    self._stored_response(row.response_json),
                )
            if str(row.lease_expires_at) > now:
                raise IdempotencyConflictError("idempotent operation is already in progress")
            updated = transaction.connection.exec_driver_sql(
                "UPDATE api_idempotency SET lease_uid=?,lease_expires_at=?,"
                "attempt_count=attempt_count+1,updated_at=? WHERE owner_uid=? AND operation=? "
                "AND key_hash=? AND operation_state='IN_PROGRESS' AND lease_expires_at<=?",
                (
                    lease_uid,
                    lease_expires,
                    now,
                    owner_uid,
                    operation,
                    key_hash,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise IdempotencyConflictError("idempotent operation lease changed")
            return IdempotencyClaim(
                owner_uid,
                operation,
                key_hash,
                request_hash,
                str(row.operation_uid),
                lease_uid,
                int(row.attempt_count) + 1,
                str(row.created_at),
            )

        try:
            claim = self._writer.submit(command).result()
        except BaseException:
            self._release_active(active_key, lease_uid)
            raise
        if claim.response is not None:
            self._release_active(active_key, lease_uid)
        return claim

    def complete(
        self,
        claim: IdempotencyClaim,
        response_status: int,
        response: dict[str, Any],
        audit_action: str,
        *,
        publish: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        now = format_rfc3339(self._clock())
        response_json = json.dumps(response, sort_keys=True, separators=(",", ":"))

        def command(transaction: TransactionContext) -> dict[str, Any]:
            row = transaction.connection.exec_driver_sql(
                "SELECT request_hash,operation_uid,operation_state,lease_uid,response_json "
                "FROM api_idempotency WHERE owner_uid=? AND operation=? AND key_hash=?",
                (claim.owner_uid, claim.operation, claim.key_hash),
            ).one()
            if row.request_hash != claim.request_hash or row.operation_uid != claim.operation_uid:
                raise IdempotencyConflictError("idempotency reservation identity changed")
            if row.operation_state == "COMPLETED":
                return self._stored_response(row.response_json)
            if claim.lease_uid is None or row.lease_uid != claim.lease_uid:
                raise IdempotencyConflictError("idempotent operation lease changed")
            if publish is not None:
                publish()
            updated = transaction.connection.exec_driver_sql(
                "UPDATE api_idempotency SET operation_state='COMPLETED',response_status=?,"
                "response_json=?,updated_at=?,completed_at=? WHERE owner_uid=? AND operation=? "
                "AND key_hash=? AND operation_state='IN_PROGRESS' AND lease_uid=?",
                (
                    response_status,
                    response_json,
                    now,
                    now,
                    claim.owner_uid,
                    claim.operation,
                    claim.key_hash,
                    claim.lease_uid,
                ),
            )
            if updated.rowcount != 1:
                raise IdempotencyConflictError("idempotent operation completion lost its lease")
            transaction.connection.exec_driver_sql(
                "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
                "detail_hash,created_at) VALUES (?,?,NULL,NULL,?,?)",
                (new_uid(), audit_action, canonical_hash(response), now),
            )
            return response

        try:
            return self._writer.submit(command).result()
        finally:
            self.release(claim)

    def fail(self, claim: IdempotencyClaim, audit_action: str, error_code: str) -> None:
        now = format_rfc3339(self._clock())
        detail_hash = canonical_hash(
            {
                "operation_uid": claim.operation_uid,
                "lease_uid": claim.lease_uid,
                "attempt_count": claim.attempt_count,
                "error_code": error_code,
            }
        )

        def command(transaction: TransactionContext) -> None:
            row = transaction.connection.exec_driver_sql(
                "SELECT request_hash,operation_uid FROM api_idempotency "
                "WHERE owner_uid=? AND operation=? AND key_hash=?",
                (claim.owner_uid, claim.operation, claim.key_hash),
            ).one()
            if row.request_hash != claim.request_hash or row.operation_uid != claim.operation_uid:
                raise IdempotencyConflictError("idempotency reservation identity changed")
            existing = transaction.connection.exec_driver_sql(
                "SELECT 1 FROM audit_record WHERE action=? AND detail_hash=? LIMIT 1",
                (audit_action, detail_hash),
            ).one_or_none()
            if existing is None:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
                    "detail_hash,created_at) VALUES (?,?,NULL,NULL,?,?)",
                    (new_uid(), audit_action, detail_hash, now),
                )

        try:
            self._writer.submit(command).result()
        finally:
            self.release(claim)

    def release(self, claim: IdempotencyClaim) -> None:
        if claim.lease_uid is None:
            return
        self._release_active(
            (claim.owner_uid, claim.operation, claim.key_hash),
            claim.lease_uid,
        )

    def replay(
        self, owner_uid: str, operation: str, idempotency_key: str, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        key_hash = canonical_hash(idempotency_key)
        request_hash = canonical_hash(request)
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT request_hash,operation_state,response_json FROM api_idempotency "
                "WHERE owner_uid=? AND operation=? AND key_hash=?",
                (owner_uid, operation, key_hash),
            ).one_or_none()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise IdempotencyConflictError("idempotency key was used for another request")
        if row.operation_state != "COMPLETED":
            raise IdempotencyConflictError("idempotent operation is already in progress")
        return self._stored_response(row.response_json)

    def _settings_response(self, row: Any) -> dict[str, Any]:
        return {
            "enabled": bool(row.enabled),
            "endpoint_source": str(row.endpoint_source),
            "endpoint_status": "CONFIGURED" if self._webhook_configured else "NOT_CONFIGURED",
            "endpoint_masked": "configured" if self._webhook_configured else None,
            "endpoint_value_status": "VALUE" if self._webhook_configured else "MISSING",
            "updated_at": str(row.updated_at),
            "version": int(row.version),
            "etag": f'"notification-settings-v{int(row.version)}"',
        }

    @staticmethod
    def _idempotent(
        transaction: TransactionContext,
        owner_uid: str,
        operation: str,
        key_hash: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        row = transaction.connection.exec_driver_sql(
            "SELECT request_hash,operation_state,response_json FROM api_idempotency "
            "WHERE owner_uid=? AND operation=? AND key_hash=?",
            (owner_uid, operation, key_hash),
        ).one_or_none()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise IdempotencyConflictError("idempotency key was used for another request")
        if row.operation_state != "COMPLETED":
            raise IdempotencyConflictError("idempotent operation is already in progress")
        return WriteService._stored_response(row.response_json)

    @staticmethod
    def _stored_response(response_json: Any) -> dict[str, Any]:
        value = json.loads(str(response_json))
        if not isinstance(value, dict):
            raise RuntimeError("stored idempotent response is invalid")
        return value

    def _release_active(self, active_key: tuple[str, str, str], lease_uid: str) -> None:
        with self._active_lock:
            if self._active_claims.get(active_key) == lease_uid:
                del self._active_claims[active_key]
