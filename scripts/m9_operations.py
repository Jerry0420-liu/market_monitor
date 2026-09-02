"""Bounded, offline M9 operations for the local Market Monitor release candidate.

The command is intentionally a small composition layer.  It does not contain market
rules or write official truth directly: database mutations are delegated to the existing
WriterQueue and domain services.  Every invocation emits exactly one JSON object on stdout;
diagnostic text and tracebacks are never emitted to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

# A script is executable from a clean source tree, where the package roots are not on
# sys.path (pytest's pythonpath setting does not apply to subprocesses).
ROOT = Path(__file__).resolve().parents[1]
for _source in reversed(
    (
        ROOT / "apps" / "api" / "src",
        ROOT / "packages" / "analysis" / "src",
        ROOT / "packages" / "contracts" / "src",
        ROOT / "packages" / "data" / "src",
        ROOT / "packages" / "notifications" / "src",
        ROOT / "packages" / "persistence" / "src",
    )
):
    sys.path.insert(0, str(_source))

from market_monitor_analysis.analysis_commit import (  # noqa: E402
    AnalysisCommitRequest,
    AnalysisCommitService,
    MetricProvenance,
)
from market_monitor_analysis.canonical import canonical_bytes, canonical_hash  # noqa: E402
from market_monitor_analysis.facts import FactExecutor  # noqa: E402
from market_monitor_analysis.snapshots import SnapshotBuilder  # noqa: E402
from market_monitor_analysis.thresholds import ThresholdRegistry  # noqa: E402
from market_monitor_data.health import (  # noqa: E402
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
    WatermarkRepository,
)
from market_monitor_data.ingestion import IngestionService  # noqa: E402
from market_monitor_data.models import ProviderBatch, ProviderRecord  # noqa: E402
from market_monitor_data.reference import ReferenceRepository  # noqa: E402
from market_monitor_notifications.adapters import InAppAdapter  # noqa: E402
from market_monitor_notifications.worker import DeliveryWorker  # noqa: E402
from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.backup import (  # noqa: E402
    BackupError,
    create_backup_set,
    verify_backup_set,
)
from market_monitor_persistence.capacity import run_capacity_profile  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.diagnostics import (  # noqa: E402
    checkpoint,
    collect_database_diagnostics,
)
from market_monitor_persistence.maintenance import (  # noqa: E402
    CapacityError,
    CapacityProfile,
    MaintenanceError,
    RetentionError,
    apply_retention,
    incremental_vacuum,
    plan_retention,
    reconcile,
)
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.recovery import (  # noqa: E402
    RecoveryError,
    complete_recovery,
    restore_backup_set,
)
from market_monitor_persistence.values import (  # noqa: E402
    format_rfc3339,
    parse_rfc3339,
    utc_now,
)
from market_monitor_persistence.writer import WriterQueue  # noqa: E402


class OperationError(RuntimeError):
    """A user-visible, already-redacted operation failure."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class _ArgumentError(ValueError):
    pass


class _HelpRequested(Exception):
    """Signal that argparse help should use the CLI's JSON output contract."""


_COMMANDS = (
    "doctor",
    "backup",
    "verify-backup",
    "restore",
    "complete-recovery",
    "reconcile",
    "retain",
    "checkpoint",
    "vacuum",
    "demo-init",
    "demo-replay",
    "capacity",
)


class _Parser(argparse.ArgumentParser):
    """argparse variant that keeps failures on the JSON stdout contract."""

    def error(self, message: str) -> NoReturn:  # noqa: D102
        raise _ArgumentError(message)

    def print_help(self, file: Any | None = None) -> None:  # noqa: D102, ARG002
        raise _HelpRequested


def _add_data_dir(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--data-dir", type=Path, required=required)


def _parser() -> _Parser:
    parser = _Parser(description="Offline Market Monitor release operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    _add_data_dir(doctor)

    backup = subparsers.add_parser("backup")
    _add_data_dir(backup)
    backup.add_argument("--destination", type=Path, required=True)

    verify = subparsers.add_parser("verify-backup")
    verify.add_argument("--path", type=Path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)

    finish = subparsers.add_parser("complete-recovery")
    _add_data_dir(finish, required=False)

    reconcile_parser = subparsers.add_parser("reconcile")
    _add_data_dir(reconcile_parser)

    retain = subparsers.add_parser("retain")
    _add_data_dir(retain)
    retain.add_argument("--cutoff", required=True)
    retain.add_argument("--apply", action="store_true")
    retain.add_argument("--expected-count", type=int, default=None)

    checkpoint_parser = subparsers.add_parser("checkpoint")
    _add_data_dir(checkpoint_parser)
    checkpoint_parser.add_argument("--mode", default="PASSIVE")

    vacuum = subparsers.add_parser("vacuum")
    _add_data_dir(vacuum)
    vacuum.add_argument("--pages", type=int, required=True)

    demo = subparsers.add_parser("demo-init")
    demo.add_argument("--data-dir", type=Path, required=True)
    demo.add_argument("--fixture", type=Path, required=True)

    replay = subparsers.add_parser("demo-replay")
    _add_data_dir(replay, required=True)
    replay.add_argument("--fixture", type=Path, required=True)

    capacity = subparsers.add_parser("capacity")
    capacity.add_argument("--data-dir", type=Path, required=True)
    capacity.add_argument("--days", type=int, required=True)
    capacity.add_argument("--instruments", type=int, required=True)
    capacity.add_argument("--interval-seconds", type=int, required=True)

    return parser


@contextmanager
def _runtime_context(data_directory: Path, *, require_existing: bool = True) -> Iterator[_Runtime]:
    data_directory = data_directory.resolve()
    database = data_directory / "market-monitor.sqlite3"
    database_exists = database.is_file()
    if require_existing and not database_exists:
        raise OperationError("DATA_DIRECTORY_INVALID", "data directory is not initialized")
    runtime: DatabaseRuntime | None = None
    writer: WriterQueue | None = None
    try:
        runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(data_directory))
        if not database_exists:
            MigrationManager().upgrade(runtime)
        else:
            MigrationManager().verify(runtime)
        writer = WriterQueue(runtime)
        writer.start()
        artifacts = ArtifactStore(runtime, writer)
        yield _Runtime(runtime, writer, artifacts)
    except OperationError:
        raise
    except Exception as error:
        raise _redacted_operation_error(error) from None
    finally:
        if writer is not None:
            writer.close()
        if runtime is not None:
            runtime.close()


class _Runtime:
    def __init__(
        self, runtime: DatabaseRuntime, writer: WriterQueue, artifacts: ArtifactStore
    ) -> None:
        self.runtime = runtime
        self.writer = writer
        self.artifacts = artifacts


def _data_directory(argument: Path | None) -> Path:
    if argument is not None:
        return argument
    value = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
    if not value:
        raise OperationError("CONFIGURATION_INVALID", "MARKET_MONITOR_DATA_DIR is required")
    return Path(value)


def _redacted_operation_error(error: BaseException) -> OperationError:
    if isinstance(error, OperationError):
        return error
    if isinstance(error, BackupError):
        message = str(error)
        if "destination already exists" in message:
            return OperationError("DESTINATION_EXISTS", "backup destination already exists")
        if "insufficient free space" in message:
            return OperationError("INSUFFICIENT_SPACE", "insufficient free space")
        return OperationError("BACKUP_FAILED", "backup operation failed")
    if isinstance(error, RecoveryError):
        message = str(error)
        if "not in RECOVERING" in message or "fresh" in message or "reconciliation" in message:
            return OperationError(
                "RECOVERY_INCOMPLETE", "recovery completion requirements are not met"
            )
        if "destination already exists" in message:
            return OperationError("TARGET_EXISTS", "restore destination must be new")
        return OperationError("RESTORE_FAILED", "restore operation failed")
    if isinstance(error, (MaintenanceError, RetentionError)):
        return OperationError("MAINTENANCE_FAILED", "maintenance operation failed")
    if isinstance(error, CapacityError):
        return OperationError("CAPACITY_INVALID", "capacity profile or target is invalid")
    if isinstance(error, (ValueError, TypeError, OSError, KeyError, json.JSONDecodeError)):
        return OperationError("OPERATION_INVALID", "operation input or durable state is invalid")
    return OperationError("OPERATION_FAILED", "operation failed")


def _load_fixture(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except OSError, UnicodeError, json.JSONDecodeError:
        raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid") from None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("batches"), list)
        or not value["batches"]
    ):
        raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid")
    result: list[dict[str, Any]] = []
    for batch in value["batches"]:
        if not isinstance(batch, dict):
            raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid")
        identifier = batch.get("id")
        received_at = batch.get("received_at")
        records = batch.get("records")
        if not isinstance(identifier, str) or not identifier or not isinstance(received_at, str):
            raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid")
        if not isinstance(records, list) or not records:
            raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid")
        normalized_records: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid")
            if not isinstance(record.get("code"), str):
                raise OperationError("FIXTURE_INVALID", "demo fixture is unavailable or invalid")
            normalized_records.append(record)
        result.append({"id": identifier, "received_at": received_at, "records": normalized_records})
    return tuple(result)


_GUARDIAN_CODES = (
    "RISE_RATE_PPM",
    "HEAD_CONCENTRATION_PPM",
    "INTERNAL_DIVERGENCE_PPM",
    "CROWDING_PPM",
    "LIQUIDITY_WEAKENING_PPM",
    "CORE_WEAKENING_PPM",
    "BREADTH_COLLAPSE_PPM",
    "STAMPEDE_RISK_PPM",
    "T1_CHASING_RISK_PPM",
    "EARLY_SIGNAL_FAILURE_PPM",
)
_SCOUT_CODES = (
    "EARLY_ACTIVITY_PPM",
    "HEALTHY_BREADTH_PPM",
    "RELATIVE_STRENGTH_PPM",
    "TURNOVER_CONFIRMATION_PPM",
    "ETF_CONFIRMATION_PPM",
    "STYLE_SUPPORT_PPM",
    "LOW_CROWDING_PPM",
    "CONTINUITY_STRENGTHENING_PPM",
)
_DEMO_RUNTIME_MARKER_KEY = "m9_demo_runtime_marker"
_DEMO_RUNTIME_MARKER_VALUE = "m9-demo-v2"
_DEMO_GUARDIAN_VERSION = "guardian-thresholds-v1.0-demo"
_DEMO_SCOUT_VERSION = "scout-thresholds-v1.0-demo"
_DEMO_METRIC_PRODUCER_VERSION = "m9-demo-metrics-v1"


def _commit_metrics() -> tuple[dict[str, int], dict[str, int]]:
    # Zero Guardian facts keep the demo risk-safe; all Scout objective facts are
    # positive observations.  Guardian still executes first and remains authoritative.
    return (
        {code: 0 for code in _GUARDIAN_CODES},
        {code: 800_000 for code in _SCOUT_CODES},
    )


def _mark_demo_runtime(context: _Runtime) -> None:
    def command(transaction: Any) -> None:
        existing = transaction.connection.exec_driver_sql(
            "SELECT value FROM system_metadata WHERE key=?",
            (_DEMO_RUNTIME_MARKER_KEY,),
        ).scalar_one_or_none()
        if existing is not None:
            if str(existing) != _DEMO_RUNTIME_MARKER_VALUE:
                raise OperationError("DEMO_RUNTIME_INVALID", "demo runtime marker conflicts")
            return
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
            (
                _DEMO_RUNTIME_MARKER_KEY,
                _DEMO_RUNTIME_MARKER_VALUE,
                format_rfc3339(utc_now()),
            ),
        )

    context.writer.submit(command).result()


def _require_demo_runtime(context: _Runtime) -> None:
    with context.runtime.read_connection() as connection:
        marker = connection.exec_driver_sql(
            "SELECT value FROM system_metadata WHERE key=?",
            (_DEMO_RUNTIME_MARKER_KEY,),
        ).scalar_one_or_none()
    if marker != _DEMO_RUNTIME_MARKER_VALUE:
        raise OperationError(
            "DEMO_RUNTIME_INVALID",
            "demo replay requires a demo-initialized data directory",
        )


def _provider_batch(
    batch: Mapping[str, Any], *, identifier: str | None = None, offset: timedelta = timedelta()
) -> ProviderBatch:
    received = str(batch["received_at"])
    records: list[ProviderRecord] = []
    for raw in batch["records"]:
        source = raw.get("source_time")
        if source is not None and offset:
            source = format_rfc3339(parse_rfc3339(str(source)) + offset)
        records.append(
            ProviderRecord(
                str(raw["code"]),
                source,
                None if raw.get("price") is None else str(raw["price"]),
                None if raw.get("volume") is None else int(raw["volume"]),
                dict(raw.get("raw", {})) if isinstance(raw.get("raw", {}), dict) else {},
            )
        )
    if offset:
        received = format_rfc3339(parse_rfc3339(received) + offset)
    return ProviderBatch(identifier or str(batch["id"]), received, tuple(records))


@dataclass(frozen=True)
class _ReferenceContext:
    instrument_uid: str
    identity_uid: str
    trading_code: str
    epoch_uid: str
    provider: str
    subject_uid: str
    bundle_uid: str


def _reference_context(context: _Runtime) -> _ReferenceContext:
    with context.runtime.read_connection() as connection:
        identity = connection.exec_driver_sql(
            "SELECT i.instrument_uid,v.identity_version_uid,v.trading_code "
            "FROM instrument i JOIN instrument_identity_version v "
            "ON v.instrument_uid=i.instrument_uid ORDER BY i.instrument_uid,v.version LIMIT 1"
        ).one_or_none()
        epoch = connection.exec_driver_sql(
            "SELECT epoch_uid,provider_key FROM market_source_epoch "
            "WHERE status='ACTIVE' ORDER BY started_at DESC,epoch_uid DESC LIMIT 1"
        ).one_or_none()
        subject = connection.exec_driver_sql(
            "SELECT subject_uid FROM analysis_subject WHERE subject_kind='MARKET' "
            "ORDER BY subject_uid LIMIT 1"
        ).scalar_one_or_none()
        bundle = connection.exec_driver_sql(
            "SELECT bundle_uid FROM reference_version_bundle ORDER BY created_at,bundle_uid LIMIT 1"
        ).scalar_one_or_none()
    if identity is None or epoch is None or subject is None or bundle is None:
        raise OperationError("REFERENCE_INVALID", "demo reference state is incomplete")
    return _ReferenceContext(
        str(identity.instrument_uid),
        str(identity.identity_version_uid),
        str(identity.trading_code),
        str(epoch.epoch_uid),
        str(epoch.provider_key),
        str(subject),
        str(bundle),
    )


def _bootstrap_demo_thresholds(context: _Runtime, at: datetime) -> None:
    with context.runtime.read_connection() as connection:
        activations = [
            (str(row.family), str(row.version))
            for row in connection.exec_driver_sql(
                "SELECT a.family,v.version FROM threshold_activation a "
                "JOIN threshold_version v ON v.threshold_version_uid=a.threshold_version_uid "
                "ORDER BY a.family,a.activation_order"
            ).all()
        ]
        if activations:
            if activations != [
                ("GUARDIAN", _DEMO_GUARDIAN_VERSION),
                ("SCOUT", _DEMO_SCOUT_VERSION),
            ]:
                raise OperationError(
                    "DEMO_THRESHOLD_INVALID",
                    "demo runtime contains non-demo threshold activation",
                )
            return
    registry = ThresholdRegistry(context.runtime, context.writer)
    for family, source_version, demo_version in (
        ("GUARDIAN", "guardian-thresholds-v1.0-prod", _DEMO_GUARDIAN_VERSION),
        ("SCOUT", "scout-thresholds-v1.0-prod", _DEMO_SCOUT_VERSION),
    ):
        source = registry.resolve_explicit(family, source_version, at)
        registry.register_version(
            family,
            demo_version,
            "OWNER_APPROVED_PENDING_VALIDATION",
            "M9 demo-only fixture; never production",
            source.entries,
            at,
        )
        for validation_kind in ("REPLAY", "SHADOW"):
            payload = json.dumps(
                {
                    "fixture": "m9-demo-threshold-bootstrap-v1",
                    "family": family,
                    "validation_kind": validation_kind,
                    "version": demo_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            artifact = context.artifacts.put_bytes(payload, "application/json")
            context.artifacts.register(artifact)
            registry.record_validation(
                family,
                demo_version,
                validation_kind,
                "PASS",
                f"m9-demo-fixture:{artifact.sha256}",
                artifact.sha256,
                at,
            )
    demo_guardian = registry.resolve_explicit("GUARDIAN", _DEMO_GUARDIAN_VERSION, at)
    demo_scout = registry.resolve_explicit("SCOUT", _DEMO_SCOUT_VERSION, at)
    evidence = context.artifacts.put_bytes(
        canonical_bytes(
            {
                "schema_version": 1,
                "evidence_kind": "M9_DEMO_ACTIVATION",
                "evidence_origin": "DEMO_FIXTURE",
                "validation_kind": "DEMO",
                "observed_at": format_rfc3339(at),
                "guardian_threshold": {
                    "uid": demo_guardian.uid,
                    "version": demo_guardian.version,
                    "definition_hash": demo_guardian.definition_hash,
                },
                "scout_threshold": {
                    "uid": demo_scout.uid,
                    "version": demo_scout.version,
                    "definition_hash": demo_scout.definition_hash,
                },
            }
        ),
        "application/vnd.market-monitor.m9-demo-activation-evidence+json",
    )
    context.artifacts.register(evidence)
    registry.record_activation_evidence(evidence.sha256, _scope="DEMO")
    registry.activate_pair(
        _DEMO_GUARDIAN_VERSION,
        _DEMO_SCOUT_VERSION,
        at,
        "m9-demo-threshold-bootstrap-v1",
        acceptance_evidence_sha256=evidence.sha256,
        _activation_scope="DEMO",
    )


def _persist_demo_metric_execution(
    context: _Runtime,
    snapshot_uid: str,
    at: datetime,
    guardian_metrics: Mapping[str, int],
    scout_metrics: Mapping[str, int],
) -> str:
    registry = ThresholdRegistry(context.runtime, context.writer)
    guardian = registry.resolve_official("GUARDIAN", at)
    scout = registry.resolve_official("SCOUT", at)
    with context.runtime.read_connection() as connection:
        snapshot = connection.exec_driver_sql(
            "SELECT s.subject_uid,s.canonical_hash AS snapshot_hash,m.manifest_uid,"
            "m.canonical_hash AS manifest_hash,m.artifact_sha256,b.bundle_uid,"
            "b.canonical_hash AS bundle_hash "
            "FROM evaluation_snapshot s JOIN input_manifest m ON m.manifest_uid=s.manifest_uid "
            "JOIN reference_version_bundle b ON b.bundle_uid=s.bundle_uid "
            "WHERE s.snapshot_uid=?",
            (snapshot_uid,),
        ).one()
        reference_entries = [
            [str(row.entity_kind), str(row.entity_uid), str(row.version_uid)]
            for row in connection.exec_driver_sql(
                "SELECT entity_kind,entity_uid,version_uid FROM reference_version_entry "
                "WHERE bundle_uid=? ORDER BY entity_kind,entity_uid,version_uid",
                (str(snapshot.bundle_uid),),
            ).all()
        ]
        source_epoch_uids = [
            str(value)
            for value in connection.exec_driver_sql(
                "SELECT DISTINCT epoch_uid FROM capability_snapshot "
                "WHERE snapshot_uid=? ORDER BY epoch_uid",
                (snapshot_uid,),
            ).scalars()
        ]
    metric_output = {
        "rule_version": _DEMO_METRIC_PRODUCER_VERSION,
        "metrics": [
            {"code": code, "status": "VALUE", "value_ppm": value}
            for code, value in sorted({**guardian_metrics, **scout_metrics}.items())
        ],
    }
    payload = {
        "schema_version": 2,
        "snapshot_uid": snapshot_uid,
        "snapshot_hash": str(snapshot.snapshot_hash),
        "subject_uid": str(snapshot.subject_uid),
        "manifest_uid": str(snapshot.manifest_uid),
        "manifest_hash": str(snapshot.manifest_hash),
        "manifest_artifact_sha256": str(snapshot.artifact_sha256),
        "bundle_uid": str(snapshot.bundle_uid),
        "bundle_hash": str(snapshot.bundle_hash),
        "reference_entries": reference_entries,
        "source_epoch_uids": source_epoch_uids,
        "producer_version": _DEMO_METRIC_PRODUCER_VERSION,
        "guardian_threshold": {
            "uid": guardian.uid,
            "version": guardian.version,
            "definition_hash": guardian.definition_hash,
        },
        "scout_threshold": {
            "uid": scout.uid,
            "version": scout.version,
            "definition_hash": scout.definition_hash,
        },
        "metric_output": metric_output,
        "metric_output_sha256": canonical_hash(metric_output),
    }
    artifact = context.artifacts.put_bytes(
        canonical_bytes(payload),
        "application/vnd.market-monitor.cr004-metric-evidence+json",
    )
    context.artifacts.register(artifact)
    facts = FactExecutor(context.runtime, context.writer)
    facts.record_metrics(
        snapshot_uid,
        "CR004_GUARDIAN_METRICS",
        _DEMO_METRIC_PRODUCER_VERSION,
        guardian_metrics,
        threshold_version_uid=guardian.uid,
        evidence_sha256=artifact.sha256,
    )
    facts.record_scout_metrics(
        snapshot_uid,
        "CR004_SCOUT_METRICS",
        _DEMO_METRIC_PRODUCER_VERSION,
        scout_metrics,
        threshold_version_uid=scout.uid,
        evidence_sha256=artifact.sha256,
    )
    return artifact.sha256


def _run_demo(
    context: _Runtime, fixture: tuple[dict[str, Any], ...], *, replay: bool
) -> dict[str, Any]:
    if replay:
        reference_context = _reference_context(context)
        instrument_uid = reference_context.instrument_uid
        identity_uid = reference_context.identity_uid
        epoch_uid = reference_context.epoch_uid
        subject_uid = reference_context.subject_uid
        bundle_uid = reference_context.bundle_uid
        with context.runtime.read_connection() as connection:
            generation = int(
                connection.exec_driver_sql(
                    "SELECT value FROM system_metadata WHERE key='restore_generation'"
                ).scalar_one_or_none()
                or 0
            )
            recovery_started = connection.exec_driver_sql(
                "SELECT value FROM system_metadata WHERE key='recovery_started_at'"
            ).scalar_one_or_none()
            batch_count = int(
                connection.exec_driver_sql("SELECT count(*) FROM market_data_batch").scalar_one()
            )
        base_time = parse_rfc3339(str(recovery_started)) if recovery_started else utc_now()
        offset = base_time + timedelta(seconds=2) - parse_rfc3339(str(fixture[0]["received_at"]))
        batches = tuple(
            _provider_batch(
                batch,
                identifier=f"{batch['id']}-replay-{generation}-{batch_count + index}",
                offset=offset,
            )
            for index, batch in enumerate(fixture)
        )
        health_time = base_time + timedelta(seconds=2)
        as_of = base_time + timedelta(seconds=30)
    else:
        now = datetime(2026, 8, 4, 1, 29, 0, tzinfo=UTC)
        references = ReferenceRepository(context.runtime, context.writer)
        instrument_uid = references.create_instrument("STOCK", now)
        identity_uid = references.add_instrument_identity_version(
            instrument_uid, "SSE", "600001", "Demo Instrument", "LISTED", "TRADING", now
        )
        references.map_instrument("DEMO_REPLAY", "600001", instrument_uid, now)
        subject_uid = references.ensure_analysis_subject("MARKET")
        epoch_uid = SourceEpochService(context.runtime, context.writer).start_epoch(
            "DEMO_REPLAY", "demo-fixture-v1", now
        )
        bundle_uid = ""
        batches = tuple(_provider_batch(batch) for batch in fixture)
        health_time = parse_rfc3339(batches[0].received_at)
        first_source = next(
            (
                parse_rfc3339(record.source_time)
                for record in batches[0].records
                if record.source_time
            ),
            health_time,
        )
        as_of = max(health_time, first_source) + timedelta(seconds=29)
        # The bundle is created after ingestion below, once the identity version exists.

    CapabilityHealthService(
        context.runtime, context.writer, HealthThresholds(900_000, 1_000, 60)
    ).record(epoch_uid, "QUOTES", 1_000_000, 1, health_time)
    ingestion = IngestionService(context.runtime, context.writer, context.artifacts)
    last_lineages: list[str] = []
    for batch in batches:
        ingestion_result = ingestion.ingest(epoch_uid, batch)
        last_lineages.extend(ingestion_result.lineages)
    if not last_lineages:
        raise OperationError("DEMO_FAILED", "demo replay produced no resolved quotes")

    with context.runtime.read_connection() as connection:
        quote_uids = [
            str(value)
            for value in connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid IN ("
                + ",".join("?" for _ in last_lineages)
                + ") "
                "AND is_current=1 ORDER BY quote_uid",
                tuple(last_lineages),
            ).scalars()
        ]
    if not quote_uids:
        raise OperationError("DEMO_FAILED", "demo replay produced no resolved quotes")
    WatermarkRepository(context.runtime, context.writer).advance(
        epoch_uid,
        "QUOTES",
        max(
            parse_rfc3339(record.source_time)
            for batch in batches
            for record in batch.records
            if record.source_time
        ),
        max(parse_rfc3339(batch.received_at) for batch in batches),
    )
    snapshots = SnapshotBuilder(context.runtime, context.writer, context.artifacts)
    if not bundle_uid:
        bundle_uid = snapshots.create_reference_bundle(
            [("INSTRUMENT", instrument_uid, identity_uid)]
        )
    manifest_uid = snapshots.create_manifest(quote_uids, as_of)
    snapshot_uid = snapshots.create_snapshot(
        subject_uid, manifest_uid, bundle_uid, "OFFICIAL", ["QUOTES"], [], max_skew_ms=1_000
    )
    snapshots.seal(snapshot_uid)
    _bootstrap_demo_thresholds(context, as_of)
    guardian_metrics, scout_metrics = _commit_metrics()
    with context.runtime.read_connection() as connection:
        expected_version = int(
            connection.exec_driver_sql(
                "SELECT version FROM current_state_projection WHERE subject_uid=?", (subject_uid,)
            ).scalar_one_or_none()
            or 0
        )
    evidence_sha256 = _persist_demo_metric_execution(
        context,
        snapshot_uid,
        as_of,
        guardian_metrics,
        scout_metrics,
    )
    committed = AnalysisCommitService(context.runtime, context.writer).commit(
        AnalysisCommitRequest(
            snapshot_uid,
            "AVAILABLE",
            "OBSERVING",
            expected_version,
            guardian_metrics,
            scout_metrics,
            ("IN_APP", "WEBHOOK"),
            metric_provenance=MetricProvenance(_DEMO_METRIC_PRODUCER_VERSION, evidence_sha256),
        )
    )
    delivered = 0
    webhook_calls = 0
    if not replay:
        adapter = InAppAdapter()

        def clock() -> datetime:
            return as_of + timedelta(seconds=1)

        worker = DeliveryWorker(
            context.runtime,
            context.writer,
            {"IN_APP": adapter},
            "m9-demo-local",
            clock,
        )
        worker.run_once()
        delivered = len(adapter.messages)
    with context.runtime.read_connection() as connection:
        delivery_attempts = int(
            connection.exec_driver_sql("SELECT count(*) FROM delivery_attempt").scalar_one()
        )
        disposition = str(
            connection.exec_driver_sql(
                "SELECT evaluation_disposition FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uid,),
            ).scalar_one()
        )
    result: dict[str, Any] = {
        "ok": True,
        "analysis_commit_uid": committed.commit_uid,
        "delivery_attempts": delivery_attempts,
        "delivered": delivered,
        "evaluation_disposition": disposition,
        "guardian_before_scout": True,
        "webhook_calls": webhook_calls,
    }
    return result


def _ensure_demo_target(data_directory: Path) -> None:
    target = data_directory.resolve()
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise OperationError("TARGET_NOT_EMPTY", "demo target must be a new empty directory")
    else:
        try:
            target.mkdir(parents=True)
        except OSError:
            raise OperationError("TARGET_INVALID", "demo target cannot be created") from None


def _doctor(data_directory: Path) -> dict[str, Any]:
    with _runtime_context(data_directory) as context:
        diagnostics = collect_database_diagnostics(context.runtime, context.artifacts)
        report = reconcile(context.runtime, context.artifacts, now=utc_now)
    return {"ok": report.ok, "diagnostics": asdict(diagnostics), "reconciliation": asdict(report)}


def _run(arguments: argparse.Namespace) -> dict[str, Any]:
    command = str(arguments.command)
    if command == "doctor":
        return _doctor(_data_directory(arguments.data_dir))
    if command == "verify-backup":
        verification = verify_backup_set(arguments.path)
        payload = {"ok": verification.ok, **asdict(verification)}
        if not verification.ok:
            raise OperationError(
                verification.error_code or "BACKUP_INVALID", "backup set verification failed"
            )
        return payload
    if command == "backup":
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            backup_result = create_backup_set(
                context.runtime, context.writer, context.artifacts, arguments.destination
            )
        return {"ok": True, **asdict(backup_result), "path": None}
    if command == "restore":
        restore_result = restore_backup_set(arguments.source, arguments.destination)
        return {
            "ok": True,
            **asdict(restore_result),
            "path": None,
            "recovery_state": "RECOVERING",
        }
    if command == "complete-recovery":
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            try:
                completion = complete_recovery(context.runtime, context.writer, context.artifacts)
            except RecoveryError as error:
                raise OperationError(
                    "RECOVERY_INCOMPLETE", "recovery completion requirements are not met"
                ) from error
        return {"ok": True, **asdict(completion)}
    if command == "reconcile":
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            report = reconcile(context.runtime, context.artifacts, now=utc_now)
        return {"ok": report.ok, **asdict(report)}
    if command == "retain":
        cutoff = parse_rfc3339(str(arguments.cutoff))
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            planned = plan_retention(context.runtime, context.artifacts, cutoff, now=utc_now)
            if not arguments.apply:
                return {"ok": True, "dry_run": True, **asdict(planned)}
            if (
                arguments.expected_count is not None
                and arguments.expected_count != planned.candidate_count
            ):
                raise OperationError(
                    "RETENTION_COUNT_MISMATCH", "retention candidate count does not match"
                )
            retention_result = apply_retention(
                context.runtime,
                context.writer,
                context.artifacts,
                planned,
                now=utc_now,
            )
        return {"ok": True, "dry_run": False, **asdict(retention_result)}
    if command == "checkpoint":
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            checkpoint_result = checkpoint(context.writer, str(arguments.mode))
        return {"ok": True, **asdict(checkpoint_result)}
    if command == "vacuum":
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            vacuum_result = incremental_vacuum(context.writer, int(arguments.pages))
        return {
            "ok": True,
            **asdict(vacuum_result),
            "reclaimed_pages": vacuum_result.reclaimed_pages,
        }
    if command == "demo-init":
        _ensure_demo_target(arguments.data_dir)
        fixture = _load_fixture(arguments.fixture)
        with _runtime_context(arguments.data_dir, require_existing=False) as context:
            _mark_demo_runtime(context)
            return _run_demo(context, fixture, replay=False)
    if command == "demo-replay":
        fixture = _load_fixture(arguments.fixture)
        with _runtime_context(_data_directory(arguments.data_dir)) as context:
            _require_demo_runtime(context)
            return _run_demo(context, fixture, replay=True)
    if command == "capacity":
        profile = CapacityProfile(
            int(arguments.days), int(arguments.instruments), int(arguments.interval_seconds)
        )
        capacity_result = run_capacity_profile(arguments.data_dir, profile)
        return {"ok": True, **asdict(capacity_result)}
    raise OperationError("COMMAND_INVALID", "unsupported operation")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        payload = _run(arguments)
        _emit(payload)
        return 0
    except _HelpRequested:
        _emit({"commands": list(_COMMANDS), "ok": True})
        return 0
    except (_ArgumentError, OperationError) as error:
        failure = (
            error
            if isinstance(error, OperationError)
            else OperationError("ARGUMENT_INVALID", str(error))
        )
        _emit({"error_code": failure.error_code, "message": failure.message, "ok": False})
        return 2
    except BaseException as error:  # bounded process boundary; no traceback reaches the user
        failure = _redacted_operation_error(error)
        _emit({"error_code": failure.error_code, "message": failure.message, "ok": False})
        return 1


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
