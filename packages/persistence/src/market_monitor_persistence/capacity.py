"""Bounded, real-schema capacity verification for the M9 local release candidate."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from market_monitor_persistence.artifacts import ArtifactStore, StoredArtifact
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.maintenance import (
    CapacityError,
    CapacityProfile,
    incremental_vacuum,
    plan_retention,
    preflight_capacity,
    reconcile,
)
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

_CAPACITY_START = datetime(2026, 8, 3, 1, 30, tzinfo=UTC)
_WRITER_BATCH_LIMIT = 15_000
_EPOCH_UID = "capacity-source-epoch"
_ARTIFACT_MEDIA_TYPE = "application/vnd.market-monitor.capacity-fixture+json"


@dataclass(frozen=True)
class CapacityRunResult:
    sample_count: int
    raw_record_count: int
    lineage_count: int
    quote_count: int
    current_quote_count: int
    correction_count: int
    batch_count: int
    writer_batch_count: int
    max_writer_batch_samples: int
    writer_batch_limit: int
    integrity_check: str
    foreign_key_issues: int
    reconciliation_issue_count: int
    retention_candidate_count: int
    durability: tuple[str, int, int]
    reopened_quote_count: int
    database_bytes: int
    wal_bytes: int
    artifact_bytes: int
    reclaimed_pages: int
    elapsed_seconds: float


@dataclass(frozen=True)
class _SampleBatch:
    tick: int
    source_time: datetime


def run_capacity_profile(
    destination: Path,
    profile: CapacityProfile,
    *,
    free_space: Callable[[Path], int] | None = None,
) -> CapacityRunResult:
    """Persist a bounded deterministic sample set through the production WriterQueue.

    The target must be new.  Preflight runs before any directory or database is created,
    then the runner uses the durable M1–M9 schema without relaxing WAL, FULL synchronous,
    or foreign-key enforcement.
    """
    preflight_capacity(profile, destination, free_space=free_space)
    target = destination.resolve()
    target.mkdir(parents=True)
    started = perf_counter()
    runtime: DatabaseRuntime | None = None
    writer: WriterQueue | None = None
    try:
        runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(target))
        MigrationManager().upgrade(runtime)
        writer = WriterQueue(runtime)
        writer.start()
        artifacts = ArtifactStore(runtime, writer)
        _bootstrap(writer, profile)

        writer_batch_count = 0
        max_writer_batch_samples = 0
        artifact_bytes = 0
        group: list[tuple[_SampleBatch, str]] = []
        group_samples = 0
        for item in _sample_batches(profile):
            artifact = _capacity_artifact(artifacts, profile, item)
            artifact_bytes += artifact.size_bytes
            group.append((item, artifact.sha256))
            group_samples += profile.instruments
            if group_samples >= _WRITER_BATCH_LIMIT:
                _write_group(writer, profile, tuple(group))
                writer_batch_count += 1
                max_writer_batch_samples = max(max_writer_batch_samples, group_samples)
                group = []
                group_samples = 0
        if group:
            _write_group(writer, profile, tuple(group))
            writer_batch_count += 1
            max_writer_batch_samples = max(max_writer_batch_samples, group_samples)

        _record_watermark(writer, profile)
        _record_representative_correction(writer, profile)
        counts = _counts(runtime)
        integrity, foreign_key_issues, durability = _durability(runtime)
        report = reconcile(runtime, artifacts, now=lambda: _CAPACITY_START + timedelta(days=6))
        retention = plan_retention(
            runtime,
            artifacts,
            _CAPACITY_START - timedelta(seconds=1),
            now=lambda: _CAPACITY_START + timedelta(days=6),
        )
        vacuum = incremental_vacuum(writer, 8)
        writer.close()
        writer = None
        runtime.close()
        runtime = None

        database_file = target / "market-monitor.sqlite3"
        database_bytes = database_file.stat().st_size
        wal_file = target / "market-monitor.sqlite3-wal"
        wal_bytes = wal_file.stat().st_size if wal_file.is_file() else 0
        reopened = DatabaseRuntime.open(DatabasePaths.from_data_directory(target))
        try:
            MigrationManager().verify(reopened)
            with reopened.read_connection() as connection:
                reopened_quote_count = int(
                    connection.exec_driver_sql("SELECT count(*) FROM market_quote").scalar_one()
                )
                restarted_integrity = str(
                    connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
                )
                restarted_foreign_keys = len(
                    connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                )
            if restarted_integrity != "ok" or restarted_foreign_keys:
                raise RuntimeError("capacity restart verification failed")
        finally:
            reopened.close()

        result = CapacityRunResult(
            sample_count=profile.sample_count,
            raw_record_count=counts["raw_market_record"],
            lineage_count=counts["quote_lineage"],
            quote_count=counts["market_quote"],
            current_quote_count=counts["current_quote"],
            correction_count=counts["correction"],
            batch_count=profile.samples_per_instrument,
            writer_batch_count=writer_batch_count,
            max_writer_batch_samples=max_writer_batch_samples,
            writer_batch_limit=_WRITER_BATCH_LIMIT,
            integrity_check=integrity,
            foreign_key_issues=foreign_key_issues,
            reconciliation_issue_count=sum(report.issue_counts.values()),
            retention_candidate_count=retention.candidate_count,
            durability=durability,
            reopened_quote_count=reopened_quote_count,
            database_bytes=database_bytes,
            wal_bytes=wal_bytes,
            artifact_bytes=artifact_bytes,
            reclaimed_pages=vacuum.reclaimed_pages,
            elapsed_seconds=perf_counter() - started,
        )
        _validate_result(result, profile)
        return result
    finally:
        if writer is not None:
            writer.close()
        if runtime is not None:
            runtime.close()


def _validate_result(result: CapacityRunResult, profile: CapacityProfile) -> None:
    expected = profile.sample_count
    if result.raw_record_count != expected:
        raise CapacityError("capacity raw record count mismatch")
    if result.lineage_count != expected or result.current_quote_count != expected:
        raise CapacityError("capacity current quote count mismatch")
    if result.quote_count != expected + 1 or result.correction_count != 1:
        raise CapacityError("capacity correction count mismatch")
    if result.batch_count != profile.samples_per_instrument:
        raise CapacityError("capacity batch count mismatch")
    if (
        not 0 < result.writer_batch_count
        or result.max_writer_batch_samples > result.writer_batch_limit
    ):
        raise CapacityError("capacity writer batch limit mismatch")
    if result.integrity_check != "ok" or result.foreign_key_issues != 0:
        raise CapacityError("capacity database integrity mismatch")
    if result.reconciliation_issue_count != 0 or result.retention_candidate_count != 0:
        raise CapacityError("capacity reconciliation mismatch")
    if result.durability != ("wal", 2, 1):
        raise CapacityError("capacity durability mismatch")
    if result.reopened_quote_count != result.quote_count:
        raise CapacityError("capacity restart count mismatch")
    if result.database_bytes <= 0 or result.artifact_bytes <= 0:
        raise CapacityError("capacity storage measurement is invalid")


def _capacity_artifact(
    artifacts: ArtifactStore, profile: CapacityProfile, batch: _SampleBatch
) -> StoredArtifact:
    payload = json.dumps(
        {
            "days": profile.days,
            "instruments": profile.instruments,
            "interval_seconds": profile.interval_seconds,
            "kind": "M9_CAPACITY",
            "records": [
                {
                    "code": _external_code(index),
                    "source_time": format_rfc3339(batch.source_time),
                }
                for index in range(profile.instruments)
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    artifact = artifacts.put_bytes(payload, _ARTIFACT_MEDIA_TYPE)
    artifacts.register(artifact)
    return artifact


def _bootstrap(writer: WriterQueue, profile: CapacityProfile) -> None:
    created_at = format_rfc3339(_CAPACITY_START)
    instrument_rows = [
        (_instrument_uid(index), "STOCK", created_at) for index in range(profile.instruments)
    ]
    mapping_rows = [
        (
            f"capacity-mapping-{index:04d}",
            "M9_CAPACITY",
            _external_code(index),
            "INSTRUMENT",
            _instrument_uid(index),
            "RESOLVED",
            created_at,
        )
        for index in range(profile.instruments)
    ]

    def command(transaction: TransactionContext) -> None:
        connection = transaction.connection
        connection.exec_driver_sql(
            "INSERT INTO market_source_epoch("
            "epoch_uid,provider_key,capability_fingerprint,started_at,ended_at,status,"
            "rewarm_required) VALUES (?,?,?, ?,NULL,'ACTIVE',0)",
            (_EPOCH_UID, "M9_CAPACITY", "m9-capacity-v1", created_at),
        )
        connection.exec_driver_sql(
            "INSERT INTO instrument(instrument_uid,instrument_kind,created_at) VALUES (?,?,?)",
            instrument_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO provider_mapping("
            "mapping_uid,provider_key,external_code,entity_kind,instrument_uid,mapping_status,"
            "valid_from) VALUES (?,?,?,?,?,?,?)",
            mapping_rows,
        )

    writer.submit(command).result()


def _sample_batches(profile: CapacityProfile) -> Iterator[_SampleBatch]:
    slots_per_day = 4 * 60 * 60 // profile.interval_seconds
    for day in range(profile.days):
        day_start = _CAPACITY_START + timedelta(days=day)
        for slot in range(slots_per_day):
            yield _SampleBatch(
                day * slots_per_day + slot,
                day_start + timedelta(seconds=slot * profile.interval_seconds),
            )


def _write_group(
    writer: WriterQueue,
    profile: CapacityProfile,
    entries: tuple[tuple[_SampleBatch, str], ...],
) -> None:
    def command(transaction: TransactionContext) -> None:
        batch_rows: list[tuple[Any, ...]] = []
        raw_rows: list[tuple[Any, ...]] = []
        lineage_rows: list[tuple[Any, ...]] = []
        quote_rows: list[tuple[Any, ...]] = []
        for entry, artifact_sha256 in entries:
            batch_uid = _batch_uid(entry.tick)
            source = format_rfc3339(entry.source_time)
            received = format_rfc3339(entry.source_time + timedelta(seconds=1))
            batch_rows.append(
                (batch_uid, _EPOCH_UID, batch_uid, artifact_sha256, received, "NORMALIZED")
            )
            for instrument_index in range(profile.instruments):
                instrument_uid = _instrument_uid(instrument_index)
                lineage_uid = _lineage_uid(entry.tick, instrument_index)
                quote_uid = _quote_uid(entry.tick, instrument_index)
                raw_rows.append(
                    (
                        batch_uid,
                        instrument_index,
                        _external_code(instrument_index),
                        source,
                        f"$.records[{instrument_index}]",
                        "RESOLVED",
                    )
                )
                lineage_rows.append(
                    (
                        lineage_uid,
                        _EPOCH_UID,
                        instrument_uid,
                        source,
                        "LAST",
                        _business_key(instrument_uid, source),
                    )
                )
                quote_rows.append(
                    (
                        quote_uid,
                        lineage_uid,
                        1,
                        batch_uid,
                        instrument_index,
                        received,
                        source,
                        source,
                        100_000 + (instrument_index % 1_000),
                        4,
                        "VALUE",
                        1_000 + instrument_index,
                        "VALUE",
                        1,
                    )
                )
        connection = transaction.connection
        connection.exec_driver_sql(
            "INSERT INTO market_data_batch("
            "batch_uid,epoch_uid,provider_batch_id,raw_artifact_sha256,received_at,status) "
            "VALUES (?,?,?,?,?,?)",
            batch_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO raw_market_record("
            "batch_uid,record_index,external_code,raw_source_time,payload_locator,mapping_status) "
            "VALUES (?,?,?,?,?,?)",
            raw_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO quote_lineage("
            "lineage_uid,epoch_uid,instrument_uid,source_time,quote_kind,business_key_sha256) "
            "VALUES (?,?,?,?,?,?)",
            lineage_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO market_quote("
            "quote_uid,lineage_uid,record_version,batch_uid,record_index,received_at,"
            "source_time_raw,source_time,price_scaled,price_scale,price_status,volume,"
            "volume_status,is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            quote_rows,
        )

    writer.submit(command).result()


def _record_watermark(writer: WriterQueue, profile: CapacityProfile) -> None:
    latest = _CAPACITY_START + timedelta(
        days=profile.days - 1,
        seconds=(4 * 60 * 60 // profile.interval_seconds - 1) * profile.interval_seconds,
    )
    event_time = format_rfc3339(latest)
    received_time = format_rfc3339(latest + timedelta(seconds=1))

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO capability_watermark("
            "epoch_uid,capability,event_time,received_time,version,rewarm_required) "
            "VALUES (?,'QUOTES',?,?,1,0)",
            (_EPOCH_UID, event_time, received_time),
        )

    writer.submit(command).result()


def _record_representative_correction(writer: WriterQueue, profile: CapacityProfile) -> None:
    lineage_uid = _lineage_uid(0, 0)
    original_quote_uid = _quote_uid(0, 0)
    correction_uid = "capacity-correction-000000-0000"
    source = format_rfc3339(_CAPACITY_START)
    received = format_rfc3339(_CAPACITY_START + timedelta(seconds=2))

    def command(transaction: TransactionContext) -> None:
        connection = transaction.connection
        connection.exec_driver_sql(
            "UPDATE market_quote SET is_current=0 WHERE quote_uid=? AND is_current=1",
            (original_quote_uid,),
        )
        connection.exec_driver_sql(
            "INSERT INTO market_quote("
            "quote_uid,lineage_uid,record_version,batch_uid,record_index,received_at,"
            "source_time_raw,source_time,price_scaled,price_scale,price_status,volume,"
            "volume_status,is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                correction_uid,
                lineage_uid,
                2,
                _batch_uid(0),
                0,
                received,
                source,
                source,
                100_001,
                4,
                "VALUE",
                1_001,
                "VALUE",
            ),
        )

    if profile.sample_count:
        writer.submit(command).result()


def _counts(runtime: DatabaseRuntime) -> dict[str, int]:
    with runtime.read_connection() as connection:
        return {
            "raw_market_record": int(
                connection.exec_driver_sql("SELECT count(*) FROM raw_market_record").scalar_one()
            ),
            "quote_lineage": int(
                connection.exec_driver_sql("SELECT count(*) FROM quote_lineage").scalar_one()
            ),
            "market_quote": int(
                connection.exec_driver_sql("SELECT count(*) FROM market_quote").scalar_one()
            ),
            "current_quote": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM market_quote WHERE is_current=1"
                ).scalar_one()
            ),
            "correction": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM (SELECT lineage_uid FROM market_quote "
                    "GROUP BY lineage_uid HAVING count(*)>1)"
                ).scalar_one()
            ),
        }


def _durability(runtime: DatabaseRuntime) -> tuple[str, int, tuple[str, int, int]]:
    with runtime.read_connection() as connection:
        integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
        foreign_key_issues = len(connection.exec_driver_sql("PRAGMA foreign_key_check").all())
        durability = (
            str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower(),
            int(connection.exec_driver_sql("PRAGMA synchronous").scalar_one()),
            int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()),
        )
    return integrity, foreign_key_issues, durability


def _instrument_uid(index: int) -> str:
    return f"capacity-instrument-{index:04d}"


def _external_code(index: int) -> str:
    return f"CAP{index:06d}"


def _batch_uid(tick: int) -> str:
    return f"capacity-batch-{tick:05d}"


def _lineage_uid(tick: int, instrument_index: int) -> str:
    return f"capacity-lineage-{tick:05d}-{instrument_index:04d}"


def _quote_uid(tick: int, instrument_index: int) -> str:
    return f"capacity-quote-{tick:05d}-{instrument_index:04d}"


def _business_key(instrument_uid: str, source_time: str) -> str:
    return sha256(f"{_EPOCH_UID}|{instrument_uid}|{source_time}|LAST".encode("ascii")).hexdigest()
