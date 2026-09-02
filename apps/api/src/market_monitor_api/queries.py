from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_analysis.facts import FactExecutor
from market_monitor_analysis.guardian import GuardianService
from market_monitor_analysis.scout import ScoutService
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_analysis.state import StateService
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_api.repository import ApiRepository


class QueryRateLimitError(RuntimeError):
    pass


class QueryService:
    def __init__(
        self,
        runtime: DatabaseRuntime,
        writer: WriterQueue,
        artifacts: ArtifactStore,
        clock: Callable[[], datetime],
        *,
        max_queries_per_minute: int = 30,
    ) -> None:
        if max_queries_per_minute <= 0:
            raise ValueError("query rate limit must be positive")
        self._runtime = runtime
        self._writer = writer
        self._artifacts = artifacts
        self._clock = clock
        self._limit = max_queries_per_minute
        self._lock = threading.Lock()

    def analyze(
        self, owner_uid: str, subject_uid: str, *, query_uid: str | None = None
    ) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise QueryRateLimitError("another analysis query is running")
        stable_query_uid = new_uid() if query_uid is None else query_uid
        if len(stable_query_uid) != 36:
            self._lock.release()
            raise ValueError("query UID must be a stable UUID")
        try:
            source_snapshot_uid, replay = self._prepare(stable_query_uid, owner_uid, subject_uid)
            if replay is not None:
                return replay
            source = self._source(subject_uid, source_snapshot_uid)
            capabilities = self._capabilities(str(source.snapshot_uid))
            builder = SnapshotBuilder(self._runtime, self._writer, self._artifacts)
            snapshot_uid = builder.create_snapshot(
                subject_uid,
                str(source.manifest_uid),
                str(source.bundle_uid),
                "USER_QUERY",
                [item[0] for item in capabilities if item[1]],
                [item[0] for item in capabilities if not item[1]],
                max_skew_ms=int(source.max_skew_ms),
            )
            builder.seal(snapshot_uid)
            (
                guardian_metrics,
                scout_metrics,
                guardian_producer_version,
                guardian_threshold_version_uid,
                scout_producer_version,
                scout_threshold_version_uid,
            ) = self._metrics(str(source.snapshot_uid))
            facts = FactExecutor(self._runtime, self._writer)
            guardian_facts = facts.record_metrics(
                snapshot_uid,
                "GUARDIAN_INPUTS",
                guardian_producer_version,
                guardian_metrics,
                threshold_version_uid=guardian_threshold_version_uid,
            )
            scout_facts = facts.record_scout_metrics(
                snapshot_uid,
                "SCOUT_INPUTS",
                scout_producer_version,
                scout_metrics,
                threshold_version_uid=scout_threshold_version_uid,
            )
            state = StateService(self._runtime, self._writer).evaluate(
                snapshot_uid,
                str(source.availability_state),
                None if source.lifecycle_state is None else str(source.lifecycle_state),
                [item.fact_uid for item in (*guardian_facts, *scout_facts)],
                int(source.projection_version),
            )
            guardian = GuardianService(self._runtime, self._writer).evaluate(state.evaluation_uid)
            scout = ScoutService(self._runtime, self._writer).evaluate(guardian.guardian_uid)
            context = ApiRepository(self._runtime, clock=self._clock).protective_context(
                state.evaluation_uid,
                guardian.guardian_uid,
                scout.scout_uid,
            )
            response = {
                "query_uid": stable_query_uid,
                "subject_uid": subject_uid,
                "source_snapshot_uid": str(source.snapshot_uid),
                "query_snapshot_uid": snapshot_uid,
                "evaluation_disposition": "USER_QUERY",
                "official_state_unchanged": True,
                "as_of_time": str(source.as_of_time),
                "availability_state": str(source.availability_state),
                "lifecycle_state": (
                    None if source.lifecycle_state is None else str(source.lifecycle_state)
                ),
                "lifecycle_value_status": (
                    "NOT_APPLICABLE" if source.lifecycle_state is None else "VALUE"
                ),
                "confidence": context.confidence.model_dump(mode="json"),
                "data_limitations": [
                    item.model_dump(mode="json") for item in context.data_quality.limitations
                ],
                "guardian": context.guardian.model_dump(mode="json"),
                "scout": context.scout.model_dump(mode="json"),
                "explanation": context.explanation.model_dump(mode="json"),
            }
            return self._complete(
                stable_query_uid,
                snapshot_uid,
                state.evaluation_uid,
                guardian.guardian_uid,
                scout.scout_uid,
                response,
            )
        except Exception as error:
            self._fail(stable_query_uid, owner_uid, subject_uid, type(error).__name__)
            raise
        finally:
            self._lock.release()

    def sector_match(self, query: str) -> dict[str, Any]:
        term = query.strip()
        if not term or len(term) > 100:
            raise ValueError("sector query must contain 1 to 100 characters")
        pattern = f"%{term}%"
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT s.sector_uid,s.sector_kind,v.name,a.subject_uid "
                "FROM sector s JOIN sector_version v "
                "ON v.sector_uid=s.sector_uid "
                "LEFT JOIN analysis_subject a ON a.sector_uid=s.sector_uid "
                "AND a.subject_kind='SECTOR' "
                "WHERE v.version=(SELECT max(x.version) FROM sector_version x "
                "WHERE x.sector_uid=s.sector_uid) AND "
                "(v.name LIKE ? OR EXISTS(SELECT 1 FROM sector_alias a "
                "WHERE a.sector_uid=s.sector_uid "
                "AND a.alias LIKE ?)) ORDER BY v.name,s.sector_uid LIMIT 20",
                (pattern, pattern),
            ).all()
        candidates: list[dict[str, Any]] = [
            {
                "sector_uid": str(row.sector_uid),
                "sector_kind": str(row.sector_kind),
                "name": str(row.name),
                "subject_uid": None if row.subject_uid is None else str(row.subject_uid),
                "subject_uid_status": "MISSING" if row.subject_uid is None else "VALUE",
            }
            for row in rows
        ]
        exact = [item for item in candidates if item["name"].casefold() == term.casefold()]
        return {
            "query": term,
            "match_status": "EXACT"
            if len(exact) == 1
            else "NONE"
            if not candidates
            else "AMBIGUOUS",
            "selected": exact[0] if len(exact) == 1 else None,
            "selected_value_status": "VALUE" if len(exact) == 1 else "MISSING",
            "candidates": candidates,
        }

    def _check_rate(self, owner_uid: str) -> None:
        since = format_rfc3339(self._clock() - timedelta(minutes=1))
        with self._runtime.read_connection() as connection:
            count = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM analysis_query_record "
                    "WHERE owner_uid=? AND created_at>=?",
                    (owner_uid, since),
                ).scalar_one()
            )
        if count >= self._limit:
            raise QueryRateLimitError("analysis query rate limit exceeded")

    def _source(self, subject_uid: str, snapshot_uid: str | None = None) -> Any:
        with self._runtime.read_connection() as connection:
            if snapshot_uid is None:
                row = connection.exec_driver_sql(
                    "SELECT s.snapshot_uid,s.manifest_uid,s.bundle_uid,s.max_skew_ms,"
                    "s.as_of_time,e.availability_state,e.lifecycle_state,"
                    "p.version AS projection_version FROM current_state_projection p "
                    "JOIN state_evaluation e ON e.evaluation_uid=p.evaluation_uid "
                    "JOIN analysis_commit c ON c.state_evaluation_uid=e.evaluation_uid "
                    "JOIN evaluation_snapshot s ON s.snapshot_uid=c.snapshot_uid "
                    "WHERE p.subject_uid=?",
                    (subject_uid,),
                ).one_or_none()
            else:
                row = connection.exec_driver_sql(
                    "SELECT s.snapshot_uid,s.manifest_uid,s.bundle_uid,s.max_skew_ms,"
                    "s.as_of_time,e.availability_state,e.lifecycle_state,"
                    "0 AS projection_version FROM analysis_commit c "
                    "JOIN evaluation_snapshot s ON s.snapshot_uid=c.snapshot_uid "
                    "JOIN state_evaluation e ON e.evaluation_uid=c.state_evaluation_uid "
                    "WHERE e.subject_uid=? AND s.snapshot_uid=?",
                    (subject_uid, snapshot_uid),
                ).one_or_none()
        if row is None:
            raise LookupError("subject has no committed OFFICIAL analysis")
        return row

    def _capabilities(self, snapshot_uid: str) -> list[tuple[str, bool]]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT capability,required FROM capability_snapshot WHERE snapshot_uid=? "
                "ORDER BY capability",
                (snapshot_uid,),
            ).all()
        return [(str(row.capability), bool(row.required)) for row in rows]

    def _metrics(
        self, snapshot_uid: str
    ) -> tuple[dict[str, int], dict[str, int], str, str, str, str]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT r.rule_key,r.rule_version,r.threshold_version_uid,"
                "f.fact_code,f.value_scaled FROM fact_record f "
                "JOIN rule_execution r ON r.rule_execution_uid=f.producer_rule_execution_uid "
                "WHERE f.snapshot_uid=? AND r.rule_key IN "
                "('CR004_GUARDIAN_METRICS','CR004_SCOUT_METRICS') "
                "AND f.value_status='VALUE'",
                (snapshot_uid,),
            ).all()
        guardian_rows = [row for row in rows if row.rule_key == "CR004_GUARDIAN_METRICS"]
        scout_rows = [row for row in rows if row.rule_key == "CR004_SCOUT_METRICS"]
        guardian = {str(row.fact_code): int(row.value_scaled) for row in guardian_rows}
        scout = {str(row.fact_code): int(row.value_scaled) for row in scout_rows}
        guardian_lineage = {
            (str(row.rule_version), str(row.threshold_version_uid))
            for row in guardian_rows
            if row.threshold_version_uid is not None
        }
        scout_lineage = {
            (str(row.rule_version), str(row.threshold_version_uid))
            for row in scout_rows
            if row.threshold_version_uid is not None
        }
        if (
            not guardian
            or not scout
            or len(guardian_lineage) != 1
            or len(scout_lineage) != 1
            or any(row.threshold_version_uid is None for row in (*guardian_rows, *scout_rows))
        ):
            raise ValueError("committed source metrics or threshold lineage are incomplete")
        guardian_producer_version, guardian_threshold_version_uid = guardian_lineage.pop()
        scout_producer_version, scout_threshold_version_uid = scout_lineage.pop()
        return (
            guardian,
            scout,
            guardian_producer_version,
            guardian_threshold_version_uid,
            scout_producer_version,
            scout_threshold_version_uid,
        )

    def _prepare(
        self,
        query_uid: str,
        owner_uid: str,
        subject_uid: str,
    ) -> tuple[str, dict[str, Any] | None]:
        existing = self._query_row(query_uid)
        if existing is not None:
            self._validate_query_identity(existing, owner_uid, subject_uid)
            if existing.query_status == "COMPLETED":
                return str(existing.source_snapshot_uid), self._stored_response(existing)
            if existing.query_status == "FAILED":
                now = format_rfc3339(self._clock())

                def restart(transaction: TransactionContext) -> None:
                    transaction.connection.exec_driver_sql(
                        "UPDATE analysis_query_record SET query_status='IN_PROGRESS',"
                        "query_snapshot_uid=NULL,state_evaluation_uid=NULL,guardian_uid=NULL,"
                        "scout_uid=NULL,response_hash=NULL,response_json=NULL,error_code=NULL,"
                        "updated_at=?,completed_at=NULL WHERE query_uid=? "
                        "AND query_status='FAILED'",
                        (now, query_uid),
                    )

                self._writer.submit(restart).result()
            return str(existing.source_snapshot_uid), None

        self._check_rate(owner_uid)
        source = self._source(subject_uid)
        source_snapshot_uid = str(source.snapshot_uid)
        now = format_rfc3339(self._clock())

        def command(transaction: TransactionContext) -> None:
            row = transaction.connection.exec_driver_sql(
                "SELECT owner_uid,subject_uid,source_snapshot_uid FROM analysis_query_record "
                "WHERE query_uid=?",
                (query_uid,),
            ).one_or_none()
            if row is not None:
                self._validate_query_identity(row, owner_uid, subject_uid)
                return
            transaction.connection.exec_driver_sql(
                "INSERT INTO analysis_query_record(query_uid,owner_uid,subject_uid,"
                "source_snapshot_uid,query_snapshot_uid,state_evaluation_uid,guardian_uid,"
                "scout_uid,query_status,response_hash,response_json,error_code,created_at,"
                "updated_at,completed_at) VALUES (?,?,?,?,NULL,NULL,NULL,NULL,'IN_PROGRESS',"
                "NULL,NULL,NULL,?,?,NULL)",
                (
                    query_uid,
                    owner_uid,
                    subject_uid,
                    source_snapshot_uid,
                    now,
                    now,
                ),
            )

        self._writer.submit(command).result()
        return source_snapshot_uid, None

    def _complete(
        self,
        query_uid: str,
        query_snapshot_uid: str,
        state_uid: str,
        guardian_uid: str,
        scout_uid: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        now = format_rfc3339(self._clock())
        response_hash = canonical_hash(response)
        response_json = json.dumps(response, sort_keys=True, separators=(",", ":"))

        def command(transaction: TransactionContext) -> dict[str, Any]:
            row = transaction.connection.exec_driver_sql(
                "SELECT query_status,response_hash,response_json FROM analysis_query_record "
                "WHERE query_uid=?",
                (query_uid,),
            ).one()
            if row.query_status == "COMPLETED":
                return self._stored_response(row)
            updated = transaction.connection.exec_driver_sql(
                "UPDATE analysis_query_record SET query_snapshot_uid=?,state_evaluation_uid=?,"
                "guardian_uid=?,scout_uid=?,query_status='COMPLETED',response_hash=?,"
                "response_json=?,error_code=NULL,updated_at=?,completed_at=? "
                "WHERE query_uid=? AND query_status='IN_PROGRESS'",
                (
                    query_snapshot_uid,
                    state_uid,
                    guardian_uid,
                    scout_uid,
                    response_hash,
                    response_json,
                    now,
                    now,
                    query_uid,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("query completion state changed")
            transaction.connection.exec_driver_sql(
                "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
                "detail_hash,created_at) VALUES (?,?,?,NULL,?,?)",
                (
                    new_uid(),
                    "USER_QUERY_COMPLETED",
                    response["subject_uid"],
                    canonical_hash(
                        {
                            "query_uid": query_uid,
                            "status": "COMPLETED",
                            "response_hash": response_hash,
                        }
                    ),
                    now,
                ),
            )
            return response

        return self._writer.submit(command).result()

    def _fail(self, query_uid: str, owner_uid: str, subject_uid: str, error_code: str) -> None:
        now = format_rfc3339(self._clock())
        detail_hash = canonical_hash(
            {"query_uid": query_uid, "status": "FAILED", "error_code": error_code}
        )

        def command(transaction: TransactionContext) -> None:
            row = transaction.connection.exec_driver_sql(
                "SELECT owner_uid,subject_uid,query_status FROM analysis_query_record "
                "WHERE query_uid=?",
                (query_uid,),
            ).one_or_none()
            if row is not None:
                self._validate_query_identity(row, owner_uid, subject_uid)
                if row.query_status == "FAILED":
                    return
                if row.query_status == "COMPLETED":
                    return
                transaction.connection.exec_driver_sql(
                    "UPDATE analysis_query_record SET query_status='FAILED',response_hash=NULL,"
                    "response_json=NULL,error_code=?,updated_at=?,completed_at=? "
                    "WHERE query_uid=? AND query_status='IN_PROGRESS'",
                    (error_code, now, now, query_uid),
                )
            exists = transaction.connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='USER_QUERY_FAILED' "
                "AND detail_hash=?",
                (detail_hash,),
            ).scalar_one()
            if not exists:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
                    "detail_hash,created_at) VALUES (?,'USER_QUERY_FAILED',?,NULL,?,?)",
                    (new_uid(), subject_uid, detail_hash, now),
                )

        self._writer.submit(command).result()

    def _query_row(self, query_uid: str) -> Any | None:
        with self._runtime.read_connection() as connection:
            return connection.exec_driver_sql(
                "SELECT owner_uid,subject_uid,source_snapshot_uid,query_status,response_hash,"
                "response_json FROM analysis_query_record WHERE query_uid=?",
                (query_uid,),
            ).one_or_none()

    @staticmethod
    def _validate_query_identity(row: Any, owner_uid: str, subject_uid: str) -> None:
        if row.owner_uid != owner_uid or row.subject_uid != subject_uid:
            raise ValueError("query UID belongs to another request")

    @staticmethod
    def _stored_response(row: Any) -> dict[str, Any]:
        value = json.loads(str(row.response_json))
        if not isinstance(value, dict) or canonical_hash(value) != row.response_hash:
            raise RuntimeError("stored query response is invalid")
        return value
