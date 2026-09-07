from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from market_monitor_persistence.artifacts import ArtifactError, ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import (
    format_rfc3339,
    new_uid,
    parse_rfc3339,
    sha256_file,
)
from market_monitor_persistence.writer import TransactionContext, WriterQueue

_MAX_INCREMENTAL_VACUUM_PAGES = 100_000
_CAPACITY_MAX_DAYS = 5
_CAPACITY_MAX_INSTRUMENTS = 1_500
_CAPACITY_MIN_INTERVAL_SECONDS = 30
_CAPACITY_SESSION_SECONDS = 4 * 60 * 60
_CAPACITY_BYTES_PER_SAMPLE = 1_024
_CAPACITY_BASE_BYTES = 256 * 1024 * 1024
_TDX_BAR_RETENTION_MAX_BATCH = 5_000

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationReport:
    issue_counts: dict[str, int]

    @property
    def ok(self) -> bool:
        return not any(self.issue_counts.values())


@dataclass(frozen=True)
class VacuumResult:
    page_count_before: int
    freelist_count_before: int
    page_count_after: int
    freelist_count_after: int

    @property
    def reclaimed_pages(self) -> int:
        return max(0, self.page_count_before - self.page_count_after)


class RetentionError(RuntimeError):
    """Raised when evidence-aware retention cannot prove a safe candidate set."""


class MaintenanceError(RuntimeError):
    """Raised when a maintenance operation cannot safely access durable storage."""


class CapacityError(RuntimeError):
    """Raised when a capacity profile is outside the verified P0 envelope."""


@dataclass(frozen=True)
class CapacityProfile:
    """The bounded P0 continuous-auction sample profile used for M9 verification."""

    days: int
    instruments: int
    interval_seconds: int

    def __post_init__(self) -> None:
        values = (self.days, self.instruments, self.interval_seconds)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise CapacityError("capacity values must be integers")
        if not 1 <= self.days <= _CAPACITY_MAX_DAYS:
            raise CapacityError("capacity days exceed the verified envelope")
        if not 1 <= self.instruments <= _CAPACITY_MAX_INSTRUMENTS:
            raise CapacityError("capacity instruments exceed the verified envelope")
        if self.interval_seconds < _CAPACITY_MIN_INTERVAL_SECONDS:
            raise CapacityError("capacity interval is below the verified envelope")
        if _CAPACITY_SESSION_SECONDS % self.interval_seconds != 0:
            raise CapacityError("capacity interval must divide the continuous-auction window")

    @property
    def samples_per_instrument(self) -> int:
        return self.days * (_CAPACITY_SESSION_SECONDS // self.interval_seconds)

    @property
    def sample_count(self) -> int:
        return self.instruments * self.samples_per_instrument

    @property
    def estimated_bytes(self) -> int:
        """Conservative preflight estimate for database, WAL, indexes, and work space."""
        return _CAPACITY_BASE_BYTES + self.sample_count * _CAPACITY_BYTES_PER_SAMPLE


@dataclass(frozen=True)
class CapacityPreflight:
    available_bytes: int
    required_bytes: int


def preflight_capacity(
    profile: CapacityProfile,
    destination: Path,
    *,
    free_space: Callable[[Path], int] | None = None,
) -> CapacityPreflight:
    """Check a new capacity target without creating it or changing SQLite settings."""
    target = destination.resolve()
    if target.exists():
        raise CapacityError("capacity target already exists")
    reader = _capacity_free_space if free_space is None else free_space
    available = reader(_nearest_existing(target.parent))
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise CapacityError("capacity free-space measurement is invalid")
    # Extra workspace covers WAL growth and an orderly close/reopen verification pass.
    required = profile.estimated_bytes + max(64 * 1024 * 1024, profile.estimated_bytes // 10)
    if available < required:
        raise CapacityError("insufficient free space for the requested capacity profile")
    return CapacityPreflight(available, required)


@dataclass(frozen=True)
class RetentionPlan:
    quote_uids: tuple[str, ...]
    artifact_sha256s: tuple[str, ...]
    candidate_count: int
    candidate_hash: str
    cutoff: str
    planned_at: str
    manifest_catalog_hash: str
    protected_manifest_lineages: tuple[str, ...]


@dataclass(frozen=True)
class RetentionResult:
    deleted_quote_count: int
    deleted_artifact_count: int
    audit_hash: str


@dataclass(frozen=True)
class TdxBarRetentionPlan:
    candidate_count: int
    candidate_hash: str
    cutoff: str
    planned_at: str
    manifest_catalog_hash: str
    protected_bar_uids: tuple[str, ...]


@dataclass(frozen=True)
class TdxBarRetentionResult:
    deleted_bar_count: int
    audit_hash: str


def plan_tdx_bar_retention(
    runtime: DatabaseRuntime,
    artifacts: ArtifactStore,
    cutoff: datetime,
    *,
    now: Any,
    batch_size: int = 1_000,
) -> TdxBarRetentionPlan:
    """Build a bounded, evidence-aware plan for old raw 1m bars."""
    current = now() if callable(now) else now
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime or zero-argument callable")
    _validate_bar_retention_batch_size(batch_size)
    cutoff_value = format_rfc3339(cutoff)
    current_value = format_rfc3339(current)
    if cutoff >= current:
        raise ValueError("retention cutoff must precede the current time")

    try:
        with runtime.read_connection() as connection:
            manifest_catalog_hash = _manifest_catalog_hash(connection)
            protected_bar_uids = _protected_tdx_bar_uids(artifacts, connection)
            candidate_count, candidate_digest = _scan_tdx_bar_candidates(
                connection,
                cutoff_value,
                protected_bar_uids,
                batch_size,
            )
    except RetentionError:
        raise
    except (
        ArtifactError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise RetentionError("tdx_bar retention evidence validation failed") from error

    candidate_hash = _tdx_bar_retention_hash(
        cutoff_value,
        candidate_count,
        candidate_digest,
        manifest_catalog_hash,
    )
    return TdxBarRetentionPlan(
        candidate_count=candidate_count,
        candidate_hash=candidate_hash,
        cutoff=cutoff_value,
        planned_at=current_value,
        manifest_catalog_hash=manifest_catalog_hash,
        protected_bar_uids=protected_bar_uids,
    )


def apply_tdx_bar_retention(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    plan: TdxBarRetentionPlan,
    *,
    now: Any,
    batch_size: int = 1_000,
) -> TdxBarRetentionResult:
    """Delete a fenced bar plan in short WriterQueue transactions."""
    current = now() if callable(now) else now
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime or zero-argument callable")
    _validate_bar_retention_batch_size(batch_size)
    try:
        cutoff = parse_rfc3339(plan.cutoff)
    except (AttributeError, TypeError, ValueError) as error:
        raise RetentionError("tdx_bar retention plan is invalid") from error
    refreshed = plan_tdx_bar_retention(
        runtime,
        artifacts,
        cutoff,
        now=current,
        batch_size=batch_size,
    )
    if not _same_tdx_bar_retention_plan(plan, refreshed):
        raise RetentionError("tdx_bar retention candidates changed; create a new plan")
    if plan.candidate_count == 0:
        _logger.info(
            json.dumps(
                {
                    "event": "tdx_bar_retention_completed",
                    "deleted_bar_count": 0,
                    "candidate_count": 0,
                    "cutoff": plan.cutoff,
                },
                sort_keys=True,
            )
        )
        return TdxBarRetentionResult(0, plan.candidate_hash)

    deleted = 0
    while deleted < plan.candidate_count:

        def delete_batch(transaction: TransactionContext) -> int:
            connection = transaction.connection
            if _manifest_catalog_hash(connection) != plan.manifest_catalog_hash:
                raise RetentionError("tdx_bar retention evidence catalog changed")
            query, parameters = _tdx_bar_candidate_query(
                plan.cutoff,
                plan.protected_bar_uids,
                batch_size,
            )
            rows = connection.exec_driver_sql(query, parameters).all()
            uids = tuple(str(row[0]) for row in rows)
            if not uids:
                return 0
            removed = _delete_values(connection, "tdx_bar", "bar_uid", uids)
            if removed != len(uids):
                raise RetentionError("tdx_bar delete count did not match the fenced batch")
            return removed

        try:
            removed = int(writer.submit(delete_batch).result())
        except RetentionError:
            raise
        except Exception as error:
            raise RetentionError("tdx_bar retention database apply failed") from error
        if removed == 0:
            raise RetentionError("tdx_bar retention ended before its fenced plan was deleted")
        deleted += removed

    if deleted != plan.candidate_count:
        raise RetentionError("tdx_bar retention delete count did not match the fenced plan")

    with runtime.read_connection() as connection:
        if _manifest_catalog_hash(connection) != plan.manifest_catalog_hash:
            raise RetentionError("tdx_bar retention evidence catalog changed")
        remaining, _ = _scan_tdx_bar_candidates(
            connection,
            plan.cutoff,
            plan.protected_bar_uids,
            batch_size,
        )
    if remaining:
        raise RetentionError("tdx_bar retention found new candidates during apply")

    applied_at = format_rfc3339(current)

    def record_audit(transaction: TransactionContext) -> None:
        if _manifest_catalog_hash(transaction.connection) != plan.manifest_catalog_hash:
            raise RetentionError("tdx_bar retention evidence catalog changed")
        transaction.connection.exec_driver_sql(
            "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
            "detail_hash,created_at) VALUES (?,'TDX_BAR_RETENTION_APPLIED',NULL,NULL,?,?)",
            (new_uid(), plan.candidate_hash, applied_at),
        )

    try:
        writer.submit(record_audit).result()
    except RetentionError:
        raise
    except Exception as error:
        raise RetentionError("tdx_bar retention audit write failed") from error
    _logger.info(
        json.dumps(
            {
                "event": "tdx_bar_retention_completed",
                "deleted_bar_count": deleted,
                "candidate_count": plan.candidate_count,
                "cutoff": plan.cutoff,
                "audit_hash": plan.candidate_hash,
            },
            sort_keys=True,
        )
    )
    return TdxBarRetentionResult(deleted, plan.candidate_hash)


def plan_retention(
    runtime: DatabaseRuntime,
    artifacts: ArtifactStore,
    cutoff: datetime,
    *,
    now: Any,
) -> RetentionPlan:
    """Build a read-only, deterministic retention plan that fails closed on evidence drift."""
    current = now() if callable(now) else now
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime or zero-argument callable")
    cutoff_value = format_rfc3339(cutoff)
    current_value = format_rfc3339(current)
    if cutoff >= current:
        raise ValueError("retention cutoff must precede the current time")

    try:
        with runtime.read_connection() as connection:
            manifest_rows = connection.exec_driver_sql(
                "SELECT manifest_uid,canonical_hash,artifact_sha256,as_of_time "
                "FROM input_manifest ORDER BY manifest_uid"
            ).all()
            catalog_hash = _canonical_hash(
                [
                    [
                        str(row.manifest_uid),
                        str(row.canonical_hash),
                        str(row.artifact_sha256),
                        str(row.as_of_time),
                    ]
                    for row in manifest_rows
                ]
            )
            manifest_lineages: set[str] = set()
            for row in manifest_rows:
                manifest_quote_uids = _read_manifest(artifacts, row)
                manifest_lineages.update(
                    _validate_manifest_quotes(connection, manifest_quote_uids, str(row.as_of_time))
                )
            quote_uids = _quote_candidates(connection, cutoff_value, manifest_lineages)
            artifact_sha256s = _artifact_candidates(connection, cutoff_value, current_value)
    except RetentionError:
        raise
    except (
        ArtifactError,
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise RetentionError("retention evidence validation failed") from error

    digest = _retention_hash(
        cutoff_value,
        tuple(quote_uids),
        tuple(artifact_sha256s),
        catalog_hash,
    )
    return RetentionPlan(
        tuple(quote_uids),
        tuple(artifact_sha256s),
        len(quote_uids) + len(artifact_sha256s),
        digest,
        cutoff_value,
        current_value,
        catalog_hash,
        tuple(sorted(manifest_lineages)),
    )


def apply_retention(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    plan: RetentionPlan,
    *,
    now: Any,
) -> RetentionResult:
    """Apply an unchanged plan through WriterQueue, then safely remove unregistered files."""
    current = now() if callable(now) else now
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime or zero-argument callable")
    try:
        cutoff = parse_rfc3339(plan.cutoff)
    except (AttributeError, ValueError) as error:
        raise RetentionError("retention plan is invalid") from error
    refreshed = plan_retention(runtime, artifacts, cutoff, now=current)
    if not _same_retention_candidates(plan, refreshed):
        raise RetentionError("retention candidates changed; create a new dry-run plan")

    applied_at = format_rfc3339(current)

    def delete_metadata(transaction: TransactionContext) -> tuple[int, tuple[str, ...]]:
        connection = transaction.connection
        catalog = _manifest_catalog_hash(connection)
        if catalog != plan.manifest_catalog_hash:
            raise RetentionError("retention evidence catalog changed")
        current_quotes = tuple(
            _quote_candidates(connection, plan.cutoff, set(plan.protected_manifest_lineages))
        )
        current_artifacts = tuple(_artifact_candidates(connection, plan.cutoff, applied_at))
        if current_quotes != plan.quote_uids or current_artifacts != plan.artifact_sha256s:
            raise RetentionError("retention candidates changed during apply")
        if (
            _retention_hash(plan.cutoff, current_quotes, current_artifacts, catalog)
            != plan.candidate_hash
        ):
            raise RetentionError("retention candidate hash is invalid")

        quote_deleted = _delete_values(connection, "market_quote", "quote_uid", current_quotes)
        _delete_expired_candidate_leases(connection, current_artifacts, applied_at)
        artifact_deleted = _delete_values(
            connection, "artifact_object", "sha256", current_artifacts
        )
        if quote_deleted != len(current_quotes) or artifact_deleted != len(current_artifacts):
            raise RetentionError("retention delete count did not match the fenced plan")
        connection.exec_driver_sql(
            "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
            "detail_hash,created_at) VALUES (?,'RETENTION_APPLIED',NULL,NULL,?,?)",
            (new_uid(), plan.candidate_hash, applied_at),
        )
        return quote_deleted, current_artifacts

    try:
        quote_deleted, removed_metadata = writer.submit(delete_metadata).result()
    except RetentionError:
        raise
    except Exception as error:
        raise RetentionError("retention database apply failed") from error

    def remove_files(transaction: TransactionContext) -> int:
        removed = 0
        for digest in removed_metadata:
            if transaction.connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_object WHERE sha256=?", (digest,)
            ).scalar_one():
                continue
            if (
                transaction.connection.exec_driver_sql(
                    "SELECT count(*) FROM market_data_batch WHERE raw_artifact_sha256=?",
                    (digest,),
                ).scalar_one()
                or transaction.connection.exec_driver_sql(
                    "SELECT count(*) FROM input_manifest WHERE artifact_sha256=?", (digest,)
                ).scalar_one()
            ):
                continue
            if transaction.connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_lease WHERE sha256=? AND valid_until>?",
                (digest, applied_at),
            ).scalar_one():
                continue
            path = artifacts.path_for(digest)
            try:
                path.unlink(missing_ok=True)
                _sync_directory(path.parent)
                removed += 1
            except OSError as error:
                raise RetentionError(
                    "retention metadata was applied but artifact cleanup is incomplete"
                ) from error
        return removed

    try:
        artifact_deleted = writer.submit(remove_files).result()
    except RetentionError:
        raise
    except Exception as error:
        raise RetentionError(
            "retention metadata was applied but artifact cleanup is incomplete"
        ) from error
    return RetentionResult(quote_deleted, artifact_deleted, plan.candidate_hash)


def _read_manifest(artifacts: ArtifactStore, row: Any) -> tuple[str, ...]:
    try:
        with artifacts.open_verified(str(row.artifact_sha256)) as stream:
            raw = stream.read()
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except RetentionError:
        raise
    except (ArtifactError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RetentionError("an Input Manifest is missing, corrupt, or malformed") from error
    if not isinstance(document, dict) or set(document) != {"as_of_time", "quote_uids"}:
        raise RetentionError("an Input Manifest has an invalid shape")
    as_of = document["as_of_time"]
    quote_uids = document["quote_uids"]
    if not isinstance(as_of, str) or as_of != str(row.as_of_time):
        raise RetentionError("an Input Manifest as-of value does not match metadata")
    try:
        parse_rfc3339(as_of)
    except ValueError as error:
        raise RetentionError("an Input Manifest as-of value is invalid") from error
    if (
        not isinstance(quote_uids, list)
        or not quote_uids
        or any(not isinstance(value, str) or not value or len(value) > 256 for value in quote_uids)
        or quote_uids != sorted(set(quote_uids))
    ):
        raise RetentionError("an Input Manifest quote list is invalid")
    try:
        canonical = _canonical_bytes(document)
    except (TypeError, ValueError) as error:
        raise RetentionError("an Input Manifest is not canonical JSON") from error
    if raw != canonical or _canonical_hash(document) != str(row.canonical_hash):
        raise RetentionError("an Input Manifest canonical hash does not match")
    return tuple(quote_uids)


def _validate_bar_retention_batch_size(batch_size: int) -> None:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("tdx_bar retention batch size must be an integer")
    if not 1 <= batch_size <= _TDX_BAR_RETENTION_MAX_BATCH:
        raise ValueError("tdx_bar retention batch size is outside the accepted bounds")


def _protected_tdx_bar_uids(
    artifacts: ArtifactStore,
    connection: Any,
) -> tuple[str, ...]:
    protected: set[str] = set()
    rows = connection.exec_driver_sql(
        "SELECT artifact_sha256 FROM input_manifest ORDER BY manifest_uid"
    ).all()
    for row in rows:
        try:
            with artifacts.open_verified(str(row.artifact_sha256)) as stream:
                raw = stream.read()
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except RetentionError:
            raise
        except (ArtifactError, OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RetentionError("an Input Manifest is missing, corrupt, or malformed") from error
        if not isinstance(document, dict):
            raise RetentionError("an Input Manifest has an invalid shape")
        for field in ("bar_uids", "realtime_current_bar_uids"):
            if field not in document:
                continue
            values = document[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value or len(value) > 256 for value in values
            ):
                raise RetentionError(f"an Input Manifest {field} list is invalid")
            protected.update(values)
    return tuple(sorted(protected))


def _tdx_bar_candidate_query(
    cutoff: str,
    protected_bar_uids: tuple[str, ...],
    limit: int | None = None,
) -> tuple[str, tuple[object, ...]]:
    clauses = ["interval_kind='1m'", "source_time < ?"]
    parameters: list[object] = [cutoff]
    for chunk in _chunks(protected_bar_uids, 400):
        placeholders = ",".join("?" for _ in chunk)
        clauses.append(f"bar_uid NOT IN ({placeholders})")
        parameters.extend(chunk)
    query = "SELECT bar_uid FROM tdx_bar WHERE " + " AND ".join(clauses)
    query += " ORDER BY bar_uid"
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    return query, tuple(parameters)


def _scan_tdx_bar_candidates(
    connection: Any,
    cutoff: str,
    protected_bar_uids: tuple[str, ...],
    batch_size: int,
) -> tuple[int, str]:
    query, parameters = _tdx_bar_candidate_query(cutoff, protected_bar_uids)
    digest = sha256()
    count = 0
    result = connection.exec_driver_sql(query, parameters)
    while rows := result.fetchmany(batch_size):
        for row in rows:
            digest.update(str(row[0]).encode("utf-8"))
            digest.update(b"\0")
            count += 1
    return count, digest.hexdigest()


def _tdx_bar_retention_hash(
    cutoff: str,
    candidate_count: int,
    candidate_digest: str,
    manifest_catalog_hash: str,
) -> str:
    return _canonical_hash(
        {
            "candidate_count": candidate_count,
            "candidate_digest": candidate_digest,
            "cutoff": cutoff,
            "manifest_catalog_hash": manifest_catalog_hash,
        }
    )


def _same_tdx_bar_retention_plan(
    left: TdxBarRetentionPlan,
    right: TdxBarRetentionPlan,
) -> bool:
    return (
        left.candidate_count == right.candidate_count
        and left.candidate_hash == right.candidate_hash
        and left.cutoff == right.cutoff
        and left.manifest_catalog_hash == right.manifest_catalog_hash
        and left.protected_bar_uids == right.protected_bar_uids
    )


def _validate_manifest_quotes(connection: Any, quote_uids: tuple[str, ...], as_of: str) -> set[str]:
    rows: list[Any] = []
    for chunk in _chunks(quote_uids):
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            connection.exec_driver_sql(
                "SELECT quote_uid,lineage_uid,received_at FROM market_quote "
                f"WHERE quote_uid IN ({placeholders})",
                chunk,
            ).all()
        )
    if len(rows) != len(quote_uids):
        raise RetentionError("an Input Manifest references an unknown quote")
    as_of_time = parse_rfc3339(as_of)
    try:
        if any(parse_rfc3339(str(row.received_at)) > as_of_time for row in rows):
            raise RetentionError("an Input Manifest references future input")
    except ValueError as error:
        raise RetentionError("an Input Manifest quote time is invalid") from error
    return {str(row.lineage_uid) for row in rows}


def _quote_candidates(connection: Any, cutoff: str, manifest_lineages: set[str]) -> list[str]:
    rows = connection.exec_driver_sql(
        "WITH expired AS (SELECT lineage_uid FROM market_quote GROUP BY lineage_uid "
        "HAVING count(*)=1 AND max(julianday(received_at))<julianday(?)) "
        "SELECT q.quote_uid,q.lineage_uid FROM market_quote q JOIN expired e "
        "ON e.lineage_uid=q.lineage_uid WHERE NOT EXISTS ("
        "SELECT 1 FROM market_quote x JOIN quote_quality_issue i "
        "ON i.quote_uid=x.quote_uid WHERE x.lineage_uid=q.lineage_uid) "
        "AND NOT EXISTS (SELECT 1 FROM market_quote x JOIN fact_specific_evidence f "
        "ON f.quote_uid=x.quote_uid WHERE x.lineage_uid=q.lineage_uid) "
        "ORDER BY q.quote_uid",
        (cutoff,),
    ).all()
    return [str(row.quote_uid) for row in rows if str(row.lineage_uid) not in manifest_lineages]


def _artifact_candidates(connection: Any, cutoff: str, now: str) -> list[str]:
    return [
        str(value)
        for value in connection.exec_driver_sql(
            "SELECT a.sha256 FROM artifact_object a WHERE julianday(a.created_at)<julianday(?) "
            "AND NOT EXISTS (SELECT 1 FROM market_data_batch b "
            "WHERE b.raw_artifact_sha256=a.sha256) AND NOT EXISTS ("
            "SELECT 1 FROM input_manifest m WHERE m.artifact_sha256=a.sha256) "
            "AND NOT EXISTS (SELECT 1 FROM threshold_validation v "
            "WHERE v.report_sha256=a.sha256) "
            "AND NOT EXISTS (SELECT 1 FROM threshold_activation_evidence e "
            "WHERE e.evidence_sha256=a.sha256 OR e.replay_evidence_sha256=a.sha256) "
            "AND NOT EXISTS (SELECT 1 FROM artifact_lease l WHERE l.sha256=a.sha256 "
            "AND julianday(l.valid_until)>julianday(?)) ORDER BY a.sha256",
            (cutoff, now),
        ).scalars()
    ]


def _manifest_catalog_hash(connection: Any) -> str:
    rows = connection.exec_driver_sql(
        "SELECT manifest_uid,canonical_hash,artifact_sha256,as_of_time "
        "FROM input_manifest ORDER BY manifest_uid"
    ).all()
    return _canonical_hash(
        [
            [
                str(row.manifest_uid),
                str(row.canonical_hash),
                str(row.artifact_sha256),
                str(row.as_of_time),
            ]
            for row in rows
        ]
    )


def _retention_hash(
    cutoff: str,
    quote_uids: tuple[str, ...],
    artifact_sha256s: tuple[str, ...],
    manifest_catalog_hash: str,
) -> str:
    return _canonical_hash(
        {
            "artifact_sha256s": list(artifact_sha256s),
            "cutoff": cutoff,
            "manifest_catalog_hash": manifest_catalog_hash,
            "quote_uids": list(quote_uids),
        }
    )


def _same_retention_candidates(left: RetentionPlan, right: RetentionPlan) -> bool:
    return (
        left.quote_uids == right.quote_uids
        and left.artifact_sha256s == right.artifact_sha256s
        and left.candidate_count == right.candidate_count
        and left.candidate_hash == right.candidate_hash
        and left.cutoff == right.cutoff
        and left.manifest_catalog_hash == right.manifest_catalog_hash
        and left.protected_manifest_lineages == right.protected_manifest_lineages
    )


def _delete_values(connection: Any, table: str, column: str, values: tuple[str, ...]) -> int:
    deleted = 0
    for chunk in _chunks(values):
        placeholders = ",".join("?" for _ in chunk)
        deleted += int(
            connection.exec_driver_sql(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})", chunk
            ).rowcount
        )
    return deleted


def _delete_expired_candidate_leases(connection: Any, digests: tuple[str, ...], now: str) -> None:
    for chunk in _chunks(digests):
        placeholders = ",".join("?" for _ in chunk)
        connection.exec_driver_sql(
            "DELETE FROM artifact_lease WHERE sha256 IN "
            f"({placeholders}) AND julianday(valid_until)<=julianday(?)",
            (*chunk, now),
        )


def _chunks(values: tuple[str, ...], size: int = 500) -> list[tuple[str, ...]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RetentionError("an Input Manifest contains duplicate keys")
        result[key] = value
    return result


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _capacity_free_space(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _nearest_existing(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise CapacityError("capacity target has no existing parent")
        candidate = parent
    if not candidate.is_dir():
        raise CapacityError("capacity target parent is not a directory")
    return candidate


def reconcile(
    runtime: DatabaseRuntime,
    artifacts: ArtifactStore,
    *,
    now: Any,
    allow_rewarm: bool = False,
) -> ReconciliationReport:
    """Read current durable state and return redacted integrity issue counts."""
    current = now() if callable(now) else now
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime or zero-argument callable")
    counts = {
        "database_integrity": 0,
        "foreign_key_drift": 0,
        "migration_drift": 0,
        "durability_drift": 0,
        "artifact_missing": 0,
        "artifact_corrupt": 0,
        "artifact_metadata_path_drift": 0,
        "artifact_orphan": 0,
        "state_projection_drift": 0,
        "event_projection_drift": 0,
        "watermark_drift": 0,
        "notification_generation_drift": 0,
        "recovery_metadata_drift": 0,
        "active_session_during_recovery": 0,
        "active_delivery_during_recovery": 0,
        "sector_subject_mapping_missing": 0,
    }

    try:
        MigrationManager().verify(runtime)
    except Exception:
        counts["migration_drift"] = 1

    with runtime.read_connection() as connection:
        integrity_rows = [
            str(value) for value in connection.exec_driver_sql("PRAGMA integrity_check").scalars()
        ]
        counts["database_integrity"] = sum(value != "ok" for value in integrity_rows)
        counts["foreign_key_drift"] = len(
            connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        )
        expected_pragmas = {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "auto_vacuum": 2,
            "wal_autocheckpoint": 0,
        }
        for name, expected in expected_pragmas.items():
            actual = connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()
            if name == "journal_mode":
                actual = str(actual).lower()
            counts["durability_drift"] += int(actual != expected)

        artifact_rows = connection.exec_driver_sql(
            "SELECT sha256,size_bytes,relative_path FROM artifact_object ORDER BY sha256"
        ).all()
        counts["state_projection_drift"] = _state_projection_drift(connection)
        counts["event_projection_drift"] = _event_projection_drift(connection)
        counts["watermark_drift"] = _watermark_drift(connection)
        if allow_rewarm:
            counts["watermark_drift"] = 0
        metadata = {
            str(row.key): str(row.value)
            for row in connection.exec_driver_sql(
                "SELECT key,value FROM system_metadata WHERE key IN "
                "('restore_generation','recovery_state','recovery_started_at',"
                "'recovery_manifest_sha256')"
            ).all()
        }
        counts["notification_generation_drift"] = _notification_generation_drift(
            connection, metadata
        )
        recovery_state = _recovery_state(metadata)
        counts["recovery_metadata_drift"] = _recovery_metadata_drift(
            connection, metadata, recovery_state
        )
        if allow_rewarm:
            counts["recovery_metadata_drift"] = 0
        if recovery_state == "RECOVERING":
            counts["active_session_during_recovery"] = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM owner_session WHERE revoked_at IS NULL"
                ).scalar_one()
            )
            counts["active_delivery_during_recovery"] = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM notification_delivery_state d "
                    "JOIN notification_intent i ON i.intent_uid=d.intent_uid "
                    "WHERE i.restore_generation!=CAST(? AS INTEGER) AND d.delivery_status IN "
                    "('PENDING','PROCESSING','RETRY_WAIT')",
                    (metadata["restore_generation"],),
                ).scalar_one()
            )
        counts["sector_subject_mapping_missing"] = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM sector s WHERE EXISTS ("
                "SELECT 1 FROM sector_version v WHERE v.sector_uid=s.sector_uid) "
                "AND NOT EXISTS (SELECT 1 FROM analysis_subject a "
                "WHERE a.subject_kind='SECTOR' AND a.sector_uid=s.sector_uid)"
            ).scalar_one()
        )

    for row in artifact_rows:
        digest = str(row.sha256)
        try:
            path = artifacts.path_for(digest)
        except ValueError:
            counts["artifact_metadata_path_drift"] += 1
            continue
        expected_relative = path.relative_to(runtime.paths.artifact_directory.resolve()).as_posix()
        if str(row.relative_path) != expected_relative:
            counts["artifact_metadata_path_drift"] += 1
        try:
            stat = path.stat()
        except OSError:
            counts["artifact_missing"] += 1
            continue
        try:
            if stat.st_size != int(row.size_bytes) or sha256_file(path) != digest:
                counts["artifact_corrupt"] += 1
        except OSError:
            counts["artifact_corrupt"] += 1
    try:
        counts["artifact_orphan"] = len(artifacts.find_orphans(timedelta(0), now=current))
    except OSError:
        counts["artifact_corrupt"] += 1

    return ReconciliationReport(counts)


def incremental_vacuum(writer: WriterQueue, pages: int) -> VacuumResult:
    if isinstance(pages, bool) or pages <= 0:
        raise ValueError("incremental vacuum page count must be positive")
    if pages > _MAX_INCREMENTAL_VACUUM_PAGES:
        raise ValueError("incremental vacuum page count exceeds the bounded maximum")

    def command(transaction: TransactionContext) -> VacuumResult:
        connection = transaction.connection
        before_pages = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
        before_free = int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one())
        connection.exec_driver_sql(f"PRAGMA incremental_vacuum({pages})")
        after_pages = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
        after_free = int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one())
        return VacuumResult(before_pages, before_free, after_pages, after_free)

    try:
        return writer.submit(command).result()
    except MaintenanceError:
        raise
    except Exception as error:
        raise MaintenanceError("maintenance operation unavailable") from error


def _state_projection_drift(connection: Any) -> int:
    expected_rows = connection.exec_driver_sql(
        "WITH ranked AS ("
        "SELECT e.subject_uid,e.availability_state,e.lifecycle_state,e.evaluation_uid,"
        "s.as_of_time,c.committed_at,c.commit_uid,row_number() OVER ("
        "PARTITION BY e.subject_uid ORDER BY c.committed_at DESC,c.commit_uid DESC) position "
        "FROM analysis_commit c JOIN state_evaluation e "
        "ON e.evaluation_uid=c.state_evaluation_uid JOIN evaluation_snapshot s "
        "ON s.snapshot_uid=e.snapshot_uid), counts AS ("
        "SELECT e.subject_uid,count(*) version FROM analysis_commit c "
        "JOIN state_evaluation e ON e.evaluation_uid=c.state_evaluation_uid "
        "GROUP BY e.subject_uid), valid AS ("
        "SELECT e.subject_uid,e.lifecycle_state,s.as_of_time,row_number() OVER ("
        "PARTITION BY e.subject_uid ORDER BY c.committed_at DESC,c.commit_uid DESC) position "
        "FROM analysis_commit c JOIN state_evaluation e "
        "ON e.evaluation_uid=c.state_evaluation_uid JOIN evaluation_snapshot s "
        "ON s.snapshot_uid=e.snapshot_uid WHERE e.availability_state='AVAILABLE') "
        "SELECT r.subject_uid,r.availability_state,r.lifecycle_state,v.lifecycle_state,"
        "v.as_of_time,r.evaluation_uid,r.as_of_time,c.version FROM ranked r "
        "JOIN counts c ON c.subject_uid=r.subject_uid LEFT JOIN valid v "
        "ON v.subject_uid=r.subject_uid AND v.position=1 WHERE r.position=1"
    ).all()
    expected = {
        str(row[0]): tuple(None if value is None else str(value) for value in row[1:7])
        + (int(row[7]),)
        for row in expected_rows
    }
    current_rows = connection.exec_driver_sql(
        "SELECT subject_uid,availability_state,effective_lifecycle_state,"
        "last_valid_lifecycle_state,last_valid_as_of_time,evaluation_uid,as_of_time,version,"
        "rewarm_required FROM current_state_projection"
    ).all()
    current = {
        str(row[0]): tuple(None if value is None else str(value) for value in row[1:7])
        + (int(row[7]), int(row[8]))
        for row in current_rows
    }
    state = _metadata_value(connection, "recovery_state")
    drift = 0
    for key in expected.keys() | current.keys():
        wanted = expected.get(key)
        actual = current.get(key)
        if wanted is None or actual is None or actual[:7] != wanted:
            drift += 1
        elif state != "RECOVERING" and actual[7] != int(wanted[0] == "WARMING_UP"):
            drift += 1
    return drift


def _event_projection_drift(connection: Any) -> int:
    expected_rows = connection.exec_driver_sql(
        "WITH ranked AS (SELECT e.related_key,e.event_uid,v.event_version_uid,"
        "v.event_status,v.version,v.created_at,row_number() OVER ("
        "PARTITION BY e.related_key ORDER BY v.created_at DESC,v.version DESC,"
        "v.event_version_uid DESC) position FROM market_event e JOIN event_version v "
        "ON v.event_uid=e.event_uid) SELECT related_key,event_uid,event_version_uid,"
        "event_status,version,created_at FROM ranked WHERE position=1"
    ).all()
    expected = {
        str(row[0]): (str(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5]))
        for row in expected_rows
    }
    current_rows = connection.exec_driver_sql(
        "SELECT related_key,event_uid,event_version_uid,event_status,version,updated_at "
        "FROM current_event_projection"
    ).all()
    current = {
        str(row[0]): (str(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5]))
        for row in current_rows
    }
    return sum(expected.get(key) != current.get(key) for key in expected.keys() | current.keys())


def _watermark_drift(connection: Any) -> int:
    expected_rows = connection.exec_driver_sql(
        "SELECT epoch_uid,source_time,received_at FROM ("
        "SELECT l.epoch_uid,q.source_time,q.received_at,row_number() OVER ("
        "PARTITION BY l.epoch_uid ORDER BY q.source_time DESC,q.received_at DESC,"
        "q.record_version DESC,q.quote_uid DESC) position FROM quote_lineage l "
        "JOIN market_quote q ON q.lineage_uid=l.lineage_uid WHERE q.source_time IS NOT NULL) "
        "WHERE position=1"
    ).all()
    expected = {
        str(row[0]): (_normalized_instant(row[1]), _normalized_instant(row[2]))
        for row in expected_rows
    }
    current_rows = connection.exec_driver_sql(
        "SELECT epoch_uid,event_time,received_time FROM capability_watermark "
        "WHERE capability='QUOTES'"
    ).all()
    current = {
        str(row[0]): (_normalized_instant(row[1]), _normalized_instant(row[2]))
        for row in current_rows
    }
    return sum(expected.get(key) != current.get(key) for key in expected.keys() | current.keys())


def _notification_generation_drift(connection: Any, metadata: dict[str, str]) -> int:
    try:
        generation = int(metadata.get("restore_generation", "0"))
    except ValueError:
        return (
            int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM notification_delivery_state WHERE delivery_status IN "
                    "('PENDING','PROCESSING','RETRY_WAIT')"
                ).scalar_one()
            )
            + 1
        )
    return int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM notification_delivery_state d JOIN notification_intent i "
            "ON i.intent_uid=d.intent_uid WHERE d.delivery_status IN "
            "('PENDING','PROCESSING','RETRY_WAIT') AND i.restore_generation!=?",
            (generation,),
        ).scalar_one()
    )


def _recovery_state(metadata: dict[str, str]) -> str:
    explicit = metadata.get("recovery_state")
    if explicit is not None:
        return explicit
    try:
        return "NORMAL" if int(metadata.get("restore_generation", "0")) == 0 else "RECOVERING"
    except ValueError:
        return "INVALID"


def _recovery_metadata_drift(connection: Any, metadata: dict[str, str], state: str) -> int:
    if state not in {"NORMAL", "RECOVERING"}:
        return 1
    if state == "NORMAL":
        return 0
    issues = 0
    try:
        issues += int(int(metadata.get("restore_generation", "0")) <= 0)
    except ValueError:
        issues += 1
    issues += int(not metadata.get("recovery_started_at"))
    manifest = metadata.get("recovery_manifest_sha256", "")
    issues += int(
        len(manifest) != 64 or any(character not in "0123456789abcdef" for character in manifest)
    )
    issues += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM market_source_epoch WHERE status='ACTIVE' AND rewarm_required!=1"
        ).scalar_one()
    )
    issues += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM capability_watermark w JOIN market_source_epoch e "
            "ON e.epoch_uid=w.epoch_uid WHERE e.status='ACTIVE' AND w.rewarm_required!=1"
        ).scalar_one()
    )
    return issues


def _metadata_value(connection: Any, key: str) -> str | None:
    value = connection.exec_driver_sql(
        "SELECT value FROM system_metadata WHERE key=?", (key,)
    ).scalar_one_or_none()
    return None if value is None else str(value)


def _normalized_instant(value: Any) -> str | None:
    return None if value is None else format_rfc3339(parse_rfc3339(str(value)))
