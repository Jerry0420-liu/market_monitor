import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_analysis.analysis_commit import (
    AnalysisCommitRequest,
    AnalysisCommitService,
    MetricProvenance,
)
from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.facts import FactExecutor
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_analysis.thresholds import ThresholdRegistry
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from sqlalchemy.exc import IntegrityError


def _sealed_snapshot(
    runtime: Any, writer: Any, artifacts: Any, *, disposition: str = "OFFICIAL"
) -> tuple[str, str]:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    instrument = references.create_instrument("STOCK", now)
    provider = f"FIXTURE-M6-{instrument}"
    code = instrument[-8:]
    identity = references.add_instrument_identity_version(
        instrument, "SSE", code, "M6 Fixture", "LISTED", "TRADING", now
    )
    references.map_instrument(provider, code, instrument, now)
    subject = references.ensure_analysis_subject("INSTRUMENT", instrument)
    epoch = SourceEpochService(runtime, writer).start_epoch(provider, "m6-v1", now)
    CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 1000, 60)).record(
        epoch, "QUOTES", 1_000_000, 1, now
    )
    ingestion = IngestionService(runtime, writer, artifacts).ingest(
        epoch,
        ProviderBatch(
            "m6-batch",
            "2026-08-04T00:00:01Z",
            (ProviderRecord(code, "2026-08-04T00:00:00Z", "10.0000", 1, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?", (ingestion.lineages[0],)
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    bundle = snapshots.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    manifest = snapshots.create_manifest([quote_uid], datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC))
    snapshot = snapshots.create_snapshot(
        subject, manifest, bundle, disposition, ["QUOTES"], [], max_skew_ms=1000
    )
    snapshots.seal(snapshot)
    return snapshot, subject


def _request(runtime: Any, snapshot_uid: str) -> AnalysisCommitRequest:
    guardian = {
        "RISE_RATE_PPM": 0,
        "HEAD_CONCENTRATION_PPM": 0,
        "INTERNAL_DIVERGENCE_PPM": 0,
        "CROWDING_PPM": 0,
        "LIQUIDITY_WEAKENING_PPM": 0,
        "CORE_WEAKENING_PPM": 0,
        "BREADTH_COLLAPSE_PPM": 0,
        "STAMPEDE_RISK_PPM": 0,
        "T1_CHASING_RISK_PPM": 0,
        "EARLY_SIGNAL_FAILURE_PPM": 0,
    }
    scout = {
        "EARLY_ACTIVITY_PPM": 800_000,
        "HEALTHY_BREADTH_PPM": 800_000,
        "RELATIVE_STRENGTH_PPM": 800_000,
    }
    with runtime.read_connection() as connection:
        evidence_sha256 = str(
            connection.exec_driver_sql(
                "SELECT m.artifact_sha256 FROM evaluation_snapshot s "
                "JOIN input_manifest m ON m.manifest_uid=s.manifest_uid WHERE s.snapshot_uid=?",
                (snapshot_uid,),
            ).scalar_one()
        )
    return AnalysisCommitRequest(
        snapshot_uid,
        "AVAILABLE",
        "OBSERVING",
        0,
        guardian,
        scout,
        metric_provenance=MetricProvenance("cr004-market-metrics-v1", evidence_sha256),
    )


def _bound_request(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    snapshot_uid: str,
    *,
    guardian_metrics: dict[str, int] | None = None,
    scout_metrics: dict[str, int] | None = None,
) -> AnalysisCommitRequest:
    request = _request(runtime, snapshot_uid)
    if guardian_metrics is not None:
        request = replace(request, guardian_metrics=guardian_metrics)
    if scout_metrics is not None:
        request = replace(request, scout_metrics=scout_metrics)
    registry = ThresholdRegistry(runtime, writer)
    at = datetime(2026, 8, 4, tzinfo=UTC)
    guardian = registry.resolve_official("GUARDIAN", at)
    scout = registry.resolve_official("SCOUT", at)
    provenance = request.metric_provenance
    assert provenance is not None
    evidence_sha256 = _metric_evidence(
        runtime,
        artifacts,
        snapshot_uid,
        provenance.producer_version,
        guardian,
        scout,
        request.guardian_metrics,
        request.scout_metrics,
    )
    provenance = MetricProvenance(provenance.producer_version, evidence_sha256)
    request = replace(request, metric_provenance=provenance)
    facts = FactExecutor(runtime, writer)
    facts.record_metrics(
        snapshot_uid,
        "CR004_GUARDIAN_METRICS",
        provenance.producer_version,
        request.guardian_metrics,
        threshold_version_uid=guardian.uid,
        evidence_sha256=provenance.evidence_sha256,
    )
    facts.record_scout_metrics(
        snapshot_uid,
        "CR004_SCOUT_METRICS",
        provenance.producer_version,
        request.scout_metrics,
        threshold_version_uid=scout.uid,
        evidence_sha256=provenance.evidence_sha256,
    )
    return request


def _metric_evidence(
    runtime: Any,
    artifacts: Any,
    snapshot_uid: str,
    producer_version: str,
    guardian: Any,
    scout: Any,
    guardian_metrics: Mapping[str, int],
    scout_metrics: Mapping[str, int],
) -> str:
    with runtime.read_connection() as connection:
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
        "rule_version": producer_version,
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
        "producer_version": producer_version,
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
    artifact = artifacts.put_bytes(
        canonical_bytes(payload),
        "application/vnd.market-monitor.cr004-metric-evidence+json",
    )
    artifacts.register(artifact)
    return str(artifact.sha256)


def _next_snapshot(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    subject_uid: str,
    second: int,
) -> str:
    with runtime.read_connection() as connection:
        bundle_uid = str(
            connection.exec_driver_sql(
                "SELECT bundle_uid FROM evaluation_snapshot WHERE subject_uid=? "
                "ORDER BY created_at "
                "LIMIT 1",
                (subject_uid,),
            ).scalar_one()
        )
        quote_uid = str(
            connection.exec_driver_sql("SELECT quote_uid FROM market_quote LIMIT 1").scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    as_of = datetime(2026, 8, 4, 0, 0, second, tzinfo=UTC)
    manifest_uid = snapshots.create_manifest([quote_uid], as_of)
    snapshot_uid = snapshots.create_snapshot(
        subject_uid, manifest_uid, bundle_uid, "OFFICIAL", ["QUOTES"], [], max_skew_ms=1000
    )
    snapshots.seal(snapshot_uid)
    return snapshot_uid


def test_formal_commit_is_atomic_idempotent_and_creates_frozen_outbox(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, subject = _sealed_snapshot(runtime, writer, artifacts)
    service = AnalysisCommitService(runtime, writer)
    request = _bound_request(runtime, writer, artifacts, snapshot)
    first = service.commit(request)
    second = service.commit(request)
    assert first == second
    assert first.event_version_uids and first.intent_uids
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM analysis_commit").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM audit_record").scalar_one() == 1
        state = connection.exec_driver_sql(
            "SELECT evaluation_disposition FROM state_evaluation WHERE evaluation_uid=?",
            (first.state_evaluation_uid,),
        ).one()
        intent = connection.exec_driver_sql(
            "SELECT channel,frozen_context_json FROM notification_intent WHERE intent_uid=?",
            (first.intent_uids[0],),
        ).one()
        projection = connection.exec_driver_sql(
            "SELECT subject_uid,event_status FROM market_event e JOIN current_event_projection p "
            "ON p.event_uid=e.event_uid"
        ).one()
    assert state.evaluation_disposition == "OFFICIAL"
    assert intent.channel == "IN_APP"
    assert '"guardian"' in intent.frozen_context_json
    assert '"confidence"' in intent.frozen_context_json
    assert '"scout"' in intent.frozen_context_json
    assert '"explanation"' in intent.frozen_context_json
    frozen_context = json.loads(intent.frozen_context_json)
    assert frozen_context["explanation"]["supporting"]
    assert frozen_context["explanation"]["contrary"] == []
    first_evidence = frozen_context["explanation"]["supporting"][0]
    assert first_evidence["fact_uid_status"] == "VALUE"
    assert first_evidence["rule_execution_uid_status"] == "VALUE"
    assert first_evidence["template_key"] == first_evidence["reason_code"]
    assert projection.subject_uid == subject and projection.event_status == "ACTIVE"


def test_late_commit_failure_rolls_back_every_analysis_and_outbox_row(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "CREATE TRIGGER reject_analysis_audit BEFORE INSERT ON audit_record "
            "BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
    ).result()
    request = _bound_request(runtime, writer, artifacts, snapshot)
    with runtime.read_connection() as connection:
        before = {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
            for table in (
                "rule_execution",
                "fact_record",
                "state_evaluation",
                "guardian_evaluation",
                "scout_evaluation",
                "analysis_commit",
                "market_event",
                "event_version",
                "notification_intent",
                "audit_record",
            )
        }
    with pytest.raises(Exception, match="injected audit failure"):
        AnalysisCommitService(runtime, writer).commit(request)
    with runtime.read_connection() as connection:
        counts = {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
            for table in (
                "rule_execution",
                "fact_record",
                "state_evaluation",
                "guardian_evaluation",
                "scout_evaluation",
                "analysis_commit",
                "market_event",
                "event_version",
                "notification_intent",
                "audit_record",
            )
        }
    assert counts == before


def test_non_official_snapshot_cannot_create_commit_event_or_intent(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts, disposition="USER_QUERY")
    with pytest.raises(ValueError, match="OFFICIAL"):
        AnalysisCommitService(runtime, writer).commit(_request(runtime, snapshot))
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM analysis_commit").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT count(*) FROM market_event").scalar_one() == 0
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM notification_intent").scalar_one() == 0
        )


def test_sustained_event_versions_resolution_recurrence_and_frozen_history(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, subject = _sealed_snapshot(runtime, writer, artifacts)
    service = AnalysisCommitService(runtime, writer)
    first = service.commit(_bound_request(runtime, writer, artifacts, snapshot))
    second_snapshot = _next_snapshot(runtime, writer, artifacts, subject, 31)
    second = service.commit(
        replace(
            _bound_request(
                runtime,
                writer,
                artifacts,
                second_snapshot,
                scout_metrics={
                    "EARLY_ACTIVITY_PPM": 900_000,
                    "HEALTHY_BREADTH_PPM": 900_000,
                    "RELATIVE_STRENGTH_PPM": 900_000,
                    "TURNOVER_CONFIRMATION_PPM": 900_000,
                },
            ),
            expected_projection_version=1,
        )
    )
    third_snapshot = _next_snapshot(runtime, writer, artifacts, subject, 32)
    third = service.commit(
        replace(
            _bound_request(runtime, writer, artifacts, third_snapshot, scout_metrics={}),
            expected_projection_version=2,
        )
    )
    fourth_snapshot = _next_snapshot(runtime, writer, artifacts, subject, 33)
    fourth = service.commit(
        replace(
            _bound_request(runtime, writer, artifacts, fourth_snapshot),
            expected_projection_version=3,
        )
    )
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "SELECT e.event_uid,v.version,v.event_status,v.change_type FROM market_event e "
            "JOIN event_version v ON v.event_uid=e.event_uid WHERE e.event_kind='SCOUT_WATCH' "
            "ORDER BY v.created_at,v.version"
        ).all()
        frozen_before = json.loads(
            connection.exec_driver_sql(
                "SELECT frozen_context_json FROM notification_intent WHERE intent_uid=?",
                (first.intent_uids[0],),
            ).scalar_one()
        )
        frozen_after = json.loads(
            connection.exec_driver_sql(
                "SELECT frozen_context_json FROM notification_intent WHERE intent_uid=?",
                (second.intent_uids[0],),
            ).scalar_one()
        )
    assert [(row.version, row.event_status, row.change_type) for row in rows] == [
        (1, "ACTIVE", "CREATED"),
        (2, "ACTIVE", "CHANGED"),
        (3, "RESOLVED", "RESOLVED"),
        (1, "ACTIVE", "CREATED"),
    ]
    assert rows[0].event_uid == rows[1].event_uid == rows[2].event_uid
    assert rows[3].event_uid != rows[0].event_uid
    assert frozen_before["scout"]["strength"] == "MEDIUM"
    assert frozen_after["scout"]["strength"] == "MEDIUM"
    assert len(frozen_before["scout"]["tags"]) == 3
    assert len(frozen_after["scout"]["tags"]) == 4
    assert third.event_version_uids and fourth.event_version_uids
    with pytest.raises(IntegrityError, match="event_version is immutable"):
        writer.submit(
            lambda transaction: transaction.connection.exec_driver_sql(
                "UPDATE event_version SET event_status='INVALIDATED' WHERE event_version_uid=?",
                (first.event_version_uids[0],),
            )
        ).result()
    with pytest.raises(IntegrityError, match="notification_intent is immutable"):
        writer.submit(
            lambda transaction: transaction.connection.exec_driver_sql(
                "UPDATE notification_intent SET frozen_context_json='{}' WHERE intent_uid=?",
                (first.intent_uids[0],),
            )
        ).result()


def test_guardian_suppression_keeps_scout_evidence_without_attention_event(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
    request = _request(runtime, snapshot)
    suppressed = _bound_request(
        runtime,
        writer,
        artifacts,
        snapshot,
        guardian_metrics={
            **request.guardian_metrics,
            "T1_CHASING_RISK_PPM": 1_000_000,
        },
    )
    result = AnalysisCommitService(runtime, writer).commit(suppressed)
    with runtime.read_connection() as connection:
        kinds = set(connection.exec_driver_sql("SELECT event_kind FROM market_event").scalars())
        scout = connection.exec_driver_sql(
            "SELECT scout_status,suppressed_by_guardian FROM scout_evaluation WHERE scout_uid=?",
            (result.scout_uid,),
        ).one()
        tags = connection.exec_driver_sql(
            "SELECT count(*) FROM scout_opportunity_tag WHERE scout_uid=?", (result.scout_uid,)
        ).scalar_one()
        contexts = [
            json.loads(value)
            for value in connection.exec_driver_sql(
                "SELECT frozen_context_json FROM notification_intent"
            ).scalars()
        ]
    assert kinds == {"GUARDIAN_RISK"}
    assert scout.scout_status == "ACTIVE" and scout.suppressed_by_guardian == 1
    assert tags == 3
    assert contexts and all(item["scout"]["suppressed_by_guardian"] for item in contexts)
    risk = contexts[0]["guardian"]["risks"][0]
    assert risk["fact_uid_status"] == "VALUE"
    assert risk["rule_execution_uid_status"] == "VALUE"
    assert risk["template_key"] == risk["reason_code"]


def test_event_intent_and_pending_delivery_survive_process_restart(
    m6_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m6_runtime
    snapshot, _ = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot)
    )
    paths = runtime.paths
    writer.close()
    runtime.close()
    reopened = DatabaseRuntime.open(paths)
    try:
        assert MigrationManager().verify(reopened) == "0014_cr003_official_cycle_journal"
        with reopened.read_connection() as connection:
            event = connection.exec_driver_sql(
                "SELECT event_status FROM event_version WHERE event_version_uid=?",
                (committed.event_version_uids[0],),
            ).scalar_one()
            intent = connection.exec_driver_sql(
                "SELECT frozen_context_hash FROM notification_intent WHERE intent_uid=?",
                (committed.intent_uids[0],),
            ).scalar_one()
            delivery = connection.exec_driver_sql(
                "SELECT delivery_status FROM notification_delivery_state WHERE intent_uid=?",
                (committed.intent_uids[0],),
            ).scalar_one()
        assert event == "ACTIVE"
        assert len(intent) == 64
        assert delivery == "PENDING"
    finally:
        reopened.close()
