from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from market_monitor_contracts.models import EvidenceItemView, confidence_from_quality
from market_monitor_persistence.artifacts import ArtifactError, ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid, parse_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.facts import _GUARDIAN_FACT_CODES, _SCOUT_FACT_CODES
from market_monitor_analysis.guardian import (
    _EFFECT_ORDER,
    _SEVERITY_ORDER,
    GuardianService,
    RiskFinding,
)
from market_monitor_analysis.guardian import (
    _RULES as GUARDIAN_RULES,
)
from market_monitor_analysis.scout import (
    _RULES as SCOUT_RULES,
)
from market_monitor_analysis.scout import (
    OpportunityFinding,
    ScoutService,
)
from market_monitor_analysis.state import (
    _LEGAL,
    IllegalLifecycleTransitionError,
    ProjectionConflictError,
)
from market_monitor_analysis.thresholds import (
    ThresholdRegistry,
    ThresholdSet,
    ThresholdUnavailableError,
)


@dataclass(frozen=True)
class MetricProvenance:
    producer_version: str
    evidence_sha256: str


@dataclass(frozen=True)
class _VerifiedMetricExecution:
    guardian_facts: tuple[tuple[str, str, int, str], ...]
    scout_facts: tuple[tuple[str, str, int, str], ...]
    guardian_metrics: Mapping[str, int]
    scout_metrics: Mapping[str, int]


class OfficialThresholdGateError(RuntimeError):
    """Raised before an OFFICIAL commit lacks approved threshold evidence."""


@dataclass(frozen=True)
class AnalysisCommitRequest:
    snapshot_uid: str
    availability_state: str
    lifecycle_state: str | None
    expected_projection_version: int
    guardian_metrics: Mapping[str, int]
    scout_metrics: Mapping[str, int]
    channels: tuple[str, ...] = ("IN_APP",)
    metric_provenance: MetricProvenance | None = None


@dataclass(frozen=True)
class AnalysisCommitResult:
    commit_uid: str
    state_evaluation_uid: str
    guardian_uid: str
    scout_uid: str
    event_version_uids: tuple[str, ...]
    intent_uids: tuple[str, ...]


class AnalysisCommitService:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer
        self._artifacts = ArtifactStore(runtime, writer)

    def commit(self, request: AnalysisCommitRequest) -> AnalysisCommitResult:
        channels = tuple(sorted(set(request.channels)))
        _validate_request(request, channels)
        provenance, evidence = _verified_metric_provenance(
            self._runtime, self._artifacts, request.metric_provenance
        )
        guardian_thresholds, scout_thresholds = self._official_thresholds(request.snapshot_uid)
        metrics = _verified_metric_execution(
            self._runtime,
            request.snapshot_uid,
            provenance,
            evidence,
            guardian_thresholds,
            scout_thresholds,
        )
        if dict(request.guardian_metrics) != dict(metrics.guardian_metrics):
            raise OfficialThresholdGateError(
                "OFFICIAL persisted Guardian metric values do not match caller metrics"
            )
        if dict(request.scout_metrics) != dict(metrics.scout_metrics):
            raise OfficialThresholdGateError(
                "OFFICIAL persisted Scout metric values do not match caller metrics"
            )
        request_hash = canonical_hash(
            {
                "snapshot_uid": request.snapshot_uid,
                "availability_state": request.availability_state,
                "lifecycle_state": request.lifecycle_state,
                "expected_projection_version": request.expected_projection_version,
                "guardian_metrics": sorted(metrics.guardian_metrics.items()),
                "scout_metrics": sorted(metrics.scout_metrics.items()),
                "channels": channels,
                "metric_provenance": (
                    provenance.producer_version,
                    provenance.evidence_sha256,
                ),
            }
        )
        existing = self._existing(request.snapshot_uid)
        if existing is not None:
            if existing[0] != request_hash:
                raise ValueError("snapshot already has a different Analysis Commit")
            return existing[1]

        def command(transaction: TransactionContext) -> AnalysisCommitResult:
            connection = transaction.connection
            raced = connection.exec_driver_sql(
                "SELECT commit_hash FROM analysis_commit WHERE snapshot_uid=?",
                (request.snapshot_uid,),
            ).scalar_one_or_none()
            if raced is not None:
                raise ValueError("snapshot was committed concurrently")
            snapshot = connection.exec_driver_sql(
                "SELECT s.snapshot_uid,s.subject_uid,s.evaluation_disposition,s.snapshot_status,"
                "s.canonical_hash,"
                "s.quality_context_uid,s.as_of_time,q.data_health_status,q.fitness_status,"
                "q.evidence_sufficiency FROM evaluation_snapshot s JOIN quality_context q "
                "ON q.quality_context_uid=s.quality_context_uid WHERE s.snapshot_uid=?",
                (request.snapshot_uid,),
            ).one()
            if snapshot.snapshot_status != "SEALED":
                raise ValueError("Analysis Commit requires a SEALED snapshot")
            if snapshot.evaluation_disposition != "OFFICIAL":
                raise ValueError("Analysis Commit requires an OFFICIAL snapshot")

            facts = [*metrics.guardian_facts, *metrics.scout_facts]
            state_uid, state_hash = _insert_state(
                connection,
                snapshot,
                request,
                facts,
            )
            guardian_uid, guardian_hash, guardian_effect, blocking, risks = _insert_guardian(
                connection,
                snapshot,
                state_uid,
                state_hash,
                request,
                facts,
                guardian_thresholds,
            )
            scout_uid, scout_hash, scout_status, scout_strength, suppressed, opportunities = (
                _insert_scout(
                    connection,
                    snapshot,
                    state_uid,
                    guardian_uid,
                    guardian_hash,
                    guardian_effect,
                    facts,
                    scout_thresholds,
                )
            )
            commit_uid = new_uid()
            connection.exec_driver_sql(
                "INSERT INTO analysis_commit(commit_uid,snapshot_uid,state_evaluation_uid,"
                "guardian_uid,scout_uid,evaluation_disposition,commit_hash,committed_at) "
                "VALUES (?,?,?,?,?,'OFFICIAL',?,?)",
                (
                    commit_uid,
                    request.snapshot_uid,
                    state_uid,
                    guardian_uid,
                    scout_uid,
                    request_hash,
                    snapshot.as_of_time,
                ),
            )
            limitations = [
                {"code": str(row.limitation_code), "detail": str(row.detail)}
                for row in connection.exec_driver_sql(
                    "SELECT limitation_code,detail FROM quality_limitation "
                    "WHERE quality_context_uid=? ORDER BY limitation_code",
                    (snapshot.quality_context_uid,),
                ).all()
            ]
            context = _frozen_context(
                connection,
                snapshot,
                request,
                guardian_uid,
                guardian_effect,
                blocking,
                risks,
                scout_uid,
                scout_status,
                scout_strength,
                suppressed,
                opportunities,
                limitations,
            )
            generation = int(
                connection.exec_driver_sql(
                    "SELECT value FROM system_metadata WHERE key='restore_generation'"
                ).scalar_one()
            )
            event_versions, intents = _decide_events(
                connection,
                snapshot,
                commit_uid,
                state_uid,
                guardian_uid,
                scout_uid,
                guardian_hash,
                scout_hash,
                guardian_effect,
                scout_status,
                suppressed,
                risks,
                opportunities,
                context,
                channels,
                generation,
            )
            connection.exec_driver_sql(
                "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
                "detail_hash,created_at) VALUES (?,?,?,?,?,?)",
                (
                    new_uid(),
                    "ANALYSIS_COMMITTED",
                    snapshot.subject_uid,
                    commit_uid,
                    canonical_hash(
                        {
                            "commit_hash": request_hash,
                            "event_versions": event_versions,
                            "intents": intents,
                        }
                    ),
                    snapshot.as_of_time,
                ),
            )
            return AnalysisCommitResult(
                commit_uid,
                state_uid,
                guardian_uid,
                scout_uid,
                tuple(event_versions),
                tuple(intents),
            )

        return self._writer.submit(command).result()

    def _existing(self, snapshot_uid: str) -> tuple[str, AnalysisCommitResult] | None:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT commit_uid,commit_hash,state_evaluation_uid,guardian_uid,scout_uid "
                "FROM analysis_commit WHERE snapshot_uid=?",
                (snapshot_uid,),
            ).one_or_none()
            if row is None:
                return None
            event_versions = tuple(
                str(value)
                for value in connection.exec_driver_sql(
                    "SELECT event_version_uid FROM event_version WHERE analysis_commit_uid=? "
                    "ORDER BY event_version_uid",
                    (row.commit_uid,),
                ).scalars()
            )
            intents = tuple(
                str(value)
                for value in connection.exec_driver_sql(
                    "SELECT n.intent_uid FROM notification_intent n JOIN event_version v "
                    "ON v.event_version_uid=n.event_version_uid WHERE v.analysis_commit_uid=? "
                    "ORDER BY n.intent_uid",
                    (row.commit_uid,),
                ).scalars()
            )
        return str(row.commit_hash), AnalysisCommitResult(
            str(row.commit_uid),
            str(row.state_evaluation_uid),
            str(row.guardian_uid),
            str(row.scout_uid),
            event_versions,
            intents,
        )

    def _official_thresholds(self, snapshot_uid: str) -> tuple[ThresholdSet, ThresholdSet]:
        with self._runtime.read_connection() as connection:
            snapshot = connection.exec_driver_sql(
                "SELECT evaluation_disposition,snapshot_status,as_of_time "
                "FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uid,),
            ).one()
            if snapshot.snapshot_status != "SEALED":
                raise ValueError("Analysis Commit requires a SEALED snapshot")
            if snapshot.evaluation_disposition != "OFFICIAL":
                raise ValueError("Analysis Commit accepts only OFFICIAL snapshots")
            registry = ThresholdRegistry(self._runtime, self._writer)
            try:
                return (
                    registry.resolve_official("GUARDIAN", parse_rfc3339(str(snapshot.as_of_time))),
                    registry.resolve_official("SCOUT", parse_rfc3339(str(snapshot.as_of_time))),
                )
            except ThresholdUnavailableError as error:
                raise OfficialThresholdGateError(
                    "OFFICIAL commit requires an active validated threshold pair"
                ) from error


def _validate_request(request: AnalysisCommitRequest, channels: tuple[str, ...]) -> None:
    if (request.availability_state == "AVAILABLE") != (request.lifecycle_state is not None):
        raise ValueError("only AVAILABLE evaluations have an effective lifecycle")
    if request.expected_projection_version < 0:
        raise ValueError("expected projection version cannot be negative")
    if not channels or any(channel not in {"IN_APP", "WEBHOOK"} for channel in channels):
        raise ValueError("notification channels must be IN_APP or WEBHOOK")
    if (
        request.metric_provenance is None
        or not request.metric_provenance.producer_version.strip()
        or not request.metric_provenance.evidence_sha256.strip()
    ):
        raise OfficialThresholdGateError(
            "OFFICIAL commit requires metric producer and evidence provenance"
        )
    _validate_metrics(request.guardian_metrics, _GUARDIAN_FACT_CODES)
    _validate_metrics(request.scout_metrics, _SCOUT_FACT_CODES)


def _verified_metric_provenance(
    runtime: DatabaseRuntime,
    artifacts: ArtifactStore,
    provenance: MetricProvenance | None,
) -> tuple[MetricProvenance, Mapping[str, Any]]:
    assert provenance is not None
    try:
        with artifacts.open_verified(provenance.evidence_sha256) as stream:
            evidence = json.load(stream)
        with runtime.read_connection() as connection:
            registered = connection.exec_driver_sql(
                "SELECT 1 FROM artifact_object WHERE sha256=?",
                (provenance.evidence_sha256,),
            ).scalar_one_or_none()
    except (ArtifactError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OfficialThresholdGateError("OFFICIAL metric evidence is unavailable") from error
    if registered is None:
        raise OfficialThresholdGateError("OFFICIAL metric evidence is not registered")
    if not isinstance(evidence, Mapping):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    return provenance, evidence


def _verified_metric_execution(
    runtime: DatabaseRuntime,
    snapshot_uid: str,
    provenance: MetricProvenance,
    evidence: Mapping[str, Any],
    guardian_thresholds: ThresholdSet,
    scout_thresholds: ThresholdSet,
) -> _VerifiedMetricExecution:
    evidence_guardian_metrics, evidence_scout_metrics = _verify_metric_evidence_lineage(
        runtime,
        snapshot_uid,
        provenance,
        evidence,
        guardian_thresholds,
        scout_thresholds,
    )
    guardian_facts, guardian_metrics = _metric_execution_facts(
        runtime,
        snapshot_uid,
        "CR004_GUARDIAN_METRICS",
        provenance,
        guardian_thresholds,
        _GUARDIAN_FACT_CODES,
    )
    scout_facts, scout_metrics = _metric_execution_facts(
        runtime,
        snapshot_uid,
        "CR004_SCOUT_METRICS",
        provenance,
        scout_thresholds,
        _SCOUT_FACT_CODES,
    )
    if dict(guardian_metrics) != dict(evidence_guardian_metrics) or dict(scout_metrics) != dict(
        evidence_scout_metrics
    ):
        raise OfficialThresholdGateError(
            "OFFICIAL persisted metric facts do not match bound artifact metrics"
        )
    return _VerifiedMetricExecution(
        guardian_facts,
        scout_facts,
        guardian_metrics,
        scout_metrics,
    )


def _verify_metric_evidence_lineage(
    runtime: DatabaseRuntime,
    snapshot_uid: str,
    provenance: MetricProvenance,
    evidence: Mapping[str, Any],
    guardian_thresholds: ThresholdSet,
    scout_thresholds: ThresholdSet,
) -> tuple[Mapping[str, int], Mapping[str, int]]:
    if evidence.get("schema_version") != 2:
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
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
    expected = {
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
        "producer_version": provenance.producer_version,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    if not _threshold_evidence_matches(evidence.get("guardian_threshold"), guardian_thresholds):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    if not _threshold_evidence_matches(evidence.get("scout_threshold"), scout_thresholds):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    output = evidence.get("metric_output")
    if not isinstance(output, Mapping):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    if output.get("rule_version") != provenance.producer_version or evidence.get(
        "metric_output_sha256"
    ) != canonical_hash(output):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    return _metrics_from_evidence(output)


def _threshold_evidence_matches(value: object, thresholds: ThresholdSet) -> bool:
    return isinstance(value, Mapping) and dict(value) == {
        "uid": thresholds.uid,
        "version": thresholds.version,
        "definition_hash": thresholds.definition_hash,
    }


def _metrics_from_evidence(
    output: Mapping[str, Any],
) -> tuple[Mapping[str, int], Mapping[str, int]]:
    values = output.get("metrics")
    if not isinstance(values, list):
        raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    guardian: dict[str, int] = {}
    scout: dict[str, int] = {}
    allowed = _GUARDIAN_FACT_CODES | _SCOUT_FACT_CODES
    for item in values:
        if not isinstance(item, Mapping) or set(item) != {"code", "status", "value_ppm"}:
            raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
        code = item.get("code")
        status = item.get("status")
        value = item.get("value_ppm")
        if not isinstance(code, str) or code not in allowed:
            raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
        target = guardian if code in _GUARDIAN_FACT_CODES else scout
        if code in target:
            raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
        if status == "VALUE":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
                raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
            target[code] = value
        elif status not in {"MISSING", "NOT_APPLICABLE"} or value is not None:
            raise OfficialThresholdGateError("OFFICIAL metric evidence lineage is invalid")
    return guardian, scout


def _metric_execution_facts(
    runtime: DatabaseRuntime,
    snapshot_uid: str,
    rule_key: str,
    provenance: MetricProvenance,
    thresholds: ThresholdSet,
    allowed_codes: set[str] | frozenset[str],
) -> tuple[tuple[tuple[str, str, int, str], ...], Mapping[str, int]]:
    with runtime.read_connection() as connection:
        executions = (
            connection.exec_driver_sql(
                "SELECT rule_execution_uid FROM rule_execution "
                "WHERE snapshot_uid=? AND rule_key=? AND rule_version=? "
                "AND threshold_version_uid=? AND validation_status='VALID' AND output_hash=? "
                "ORDER BY rule_execution_uid",
                (
                    snapshot_uid,
                    rule_key,
                    provenance.producer_version,
                    thresholds.uid,
                    provenance.evidence_sha256,
                ),
            )
            .scalars()
            .all()
        )
        if len(executions) != 1:
            raise OfficialThresholdGateError(
                f"OFFICIAL commit requires exactly one {rule_key} CR004 execution"
            )
        rows = connection.exec_driver_sql(
            "SELECT fact_uid,fact_code,value_scaled,value_scale,value_status,unit,fact_hash "
            "FROM fact_record WHERE producer_rule_execution_uid=? AND snapshot_uid=? "
            "ORDER BY fact_code",
            (str(executions[0]), snapshot_uid),
        ).all()
    facts: list[tuple[str, str, int, str]] = []
    metrics: dict[str, int] = {}
    for row in rows:
        code = str(row.fact_code)
        if (
            code not in allowed_codes
            or str(row.value_status) != "VALUE"
            or int(row.value_scale) != 0
            or str(row.unit) != "PPM"
            or code in metrics
        ):
            raise OfficialThresholdGateError("OFFICIAL CR004 metric fact lineage is invalid")
        value = int(row.value_scaled)
        if not 0 <= value <= 1_000_000:
            raise OfficialThresholdGateError("OFFICIAL CR004 metric value is invalid")
        metrics[code] = value
        facts.append((str(row.fact_uid), code, value, str(row.fact_hash)))
    return tuple(facts), metrics


def _validate_metrics(metrics: Mapping[str, int], allowed: set[str]) -> None:
    if any(code not in allowed for code in metrics):
        raise ValueError("metric code is not approved")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000
        for value in metrics.values()
    ):
        raise ValueError("metrics must be integer PPM values from 0 to 1000000")


def _insert_state(
    connection: Any,
    snapshot: Any,
    request: AnalysisCommitRequest,
    facts: list[tuple[str, str, int, str]],
) -> tuple[str, str]:
    if not facts:
        raise ValueError("state evaluation requires fact evidence")
    evaluation_hash = canonical_hash(
        {
            "snapshot_hash": snapshot.canonical_hash,
            "disposition": "OFFICIAL",
            "availability": request.availability_state,
            "lifecycle": request.lifecycle_state,
            "fact_hashes": sorted(item[3] for item in facts),
        }
    )
    evaluation_uid = new_uid()
    current = connection.exec_driver_sql(
        "SELECT effective_lifecycle_state,last_valid_lifecycle_state,last_valid_as_of_time,version "
        "FROM current_state_projection WHERE subject_uid=?",
        (snapshot.subject_uid,),
    ).one_or_none()
    actual_version = 0 if current is None else int(current.version)
    if actual_version != request.expected_projection_version:
        raise ProjectionConflictError(
            f"projection version is {actual_version}; "
            f"expected {request.expected_projection_version}"
        )
    previous = (
        None
        if current is None
        else current.effective_lifecycle_state or current.last_valid_lifecycle_state
    )
    if request.lifecycle_state is not None and previous not in {None, request.lifecycle_state}:
        if request.lifecycle_state not in _LEGAL[str(previous)]:
            raise IllegalLifecycleTransitionError(
                f"illegal lifecycle transition {previous} -> {request.lifecycle_state}"
            )
    connection.exec_driver_sql(
        "INSERT INTO state_evaluation(evaluation_uid,snapshot_uid,subject_uid,"
        "evaluation_disposition,availability_state,lifecycle_state,evaluation_hash,created_at) "
        "VALUES (?,?,?,'OFFICIAL',?,?,?,?)",
        (
            evaluation_uid,
            request.snapshot_uid,
            snapshot.subject_uid,
            request.availability_state,
            request.lifecycle_state,
            evaluation_hash,
            snapshot.as_of_time,
        ),
    )
    for fact_uid, _, _, _ in facts:
        connection.exec_driver_sql(
            "INSERT INTO state_evaluation_fact(evaluation_uid,fact_uid) VALUES (?,?)",
            (evaluation_uid, fact_uid),
        )
    if request.lifecycle_state is not None and previous != request.lifecycle_state:
        connection.exec_driver_sql(
            "INSERT INTO state_transition(transition_uid,subject_uid,from_lifecycle_state,"
            "to_lifecycle_state,evaluation_uid,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                new_uid(),
                snapshot.subject_uid,
                previous,
                request.lifecycle_state,
                evaluation_uid,
                snapshot.as_of_time,
            ),
        )
    last_valid = (
        request.lifecycle_state
        if request.lifecycle_state is not None
        else None
        if current is None
        else current.last_valid_lifecycle_state
    )
    last_valid_time = (
        snapshot.as_of_time
        if request.lifecycle_state is not None
        else None
        if current is None
        else current.last_valid_as_of_time
    )
    version = actual_version + 1
    values = (
        request.availability_state,
        request.lifecycle_state,
        last_valid,
        last_valid_time,
        evaluation_uid,
        snapshot.as_of_time,
        version,
        int(request.availability_state == "WARMING_UP"),
    )
    if current is None:
        connection.exec_driver_sql(
            "INSERT INTO current_state_projection(subject_uid,availability_state,"
            "effective_lifecycle_state,last_valid_lifecycle_state,last_valid_as_of_time,"
            "evaluation_uid,as_of_time,version,rewarm_required) VALUES (?,?,?,?,?,?,?,?,?)",
            (snapshot.subject_uid, *values),
        )
    else:
        updated = connection.exec_driver_sql(
            "UPDATE current_state_projection SET availability_state=?,effective_lifecycle_state=?,"
            "last_valid_lifecycle_state=?,last_valid_as_of_time=?,evaluation_uid=?,as_of_time=?,"
            "version=?,rewarm_required=? WHERE subject_uid=? AND version=?",
            (*values, snapshot.subject_uid, actual_version),
        )
        if updated.rowcount != 1:
            raise ProjectionConflictError("projection changed during Analysis Commit")
    return evaluation_uid, evaluation_hash


def _insert_guardian(
    connection: Any,
    snapshot: Any,
    state_uid: str,
    state_hash: str,
    request: AnalysisCommitRequest,
    facts: list[tuple[str, str, int, str]],
    thresholds: ThresholdSet,
) -> tuple[str, str, str, bool, list[RiskFinding]]:
    by_code = {code: (uid, value) for uid, code, value, _ in facts}
    missing = {rule[0] for rule in GUARDIAN_RULES} - by_code.keys()
    unavailable = (
        request.availability_state != "AVAILABLE"
        or snapshot.data_health_status in {"UNKNOWN", "UNHEALTHY"}
        or snapshot.fitness_status in {"UNKNOWN", "UNFIT"}
        or bool(missing)
    )
    limited = (
        snapshot.data_health_status == "DEGRADED"
        or snapshot.fitness_status == "FIT_WITH_LIMITATIONS"
    )
    risks: list[RiskFinding] = []
    effects: list[str] = []
    if unavailable or limited:
        risks.append(
            RiskFinding(
                "DATA_LIMITATION",
                "CRITICAL" if unavailable else "WARNING",
                "REQUIRED_GUARDIAN_FACTS_MISSING"
                if missing
                else "QUALITY_REQUIRES_PAUSE"
                if unavailable
                else "QUALITY_LIMITED",
                None,
            )
        )
        effects.append("PAUSE" if unavailable else "ALLOW_WITH_WARNING")
    if not unavailable:
        for code, tag, severity, rule_effect in GUARDIAN_RULES:
            fact = by_code.get(code)
            if fact is not None and fact[1] >= thresholds.entries[code]:
                risks.append(RiskFinding(tag, severity, f"{tag}_THRESHOLD_MET", fact[0]))
                effects.append(rule_effect)
    effect = max(effects, key=_EFFECT_ORDER.__getitem__) if effects else "ALLOW"
    severity = (
        max((risk.severity for risk in risks), key=_SEVERITY_ORDER.__getitem__) if risks else "NONE"
    )
    guardian_hash = canonical_hash(
        {
            "state_evaluation_hash": state_hash,
            "rule_version": GuardianService.RULE_VERSION,
            "effect": effect,
            "risks": [
                (risk.risk_tag, risk.severity, risk.reason_code, risk.primary_fact_uid)
                for risk in sorted(risks, key=lambda value: value.risk_tag)
            ],
        }
    )
    guardian_uid = new_uid()
    blocking = effect in {"SUPPRESS", "PAUSE"}
    connection.exec_driver_sql(
        "INSERT INTO guardian_evaluation(guardian_uid,state_evaluation_uid,subject_uid,"
        "snapshot_uid,rule_version,guardian_effect,blocking,max_severity,evaluation_hash,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            guardian_uid,
            state_uid,
            snapshot.subject_uid,
            request.snapshot_uid,
            GuardianService.RULE_VERSION,
            effect,
            int(blocking),
            severity,
            guardian_hash,
            snapshot.as_of_time,
        ),
    )
    for risk in risks:
        connection.exec_driver_sql(
            "INSERT INTO guardian_risk_tag(guardian_uid,risk_tag,severity,reason_code,"
            "primary_fact_uid) VALUES (?,?,?,?,?)",
            (
                guardian_uid,
                risk.risk_tag,
                risk.severity,
                risk.reason_code,
                risk.primary_fact_uid,
            ),
        )
        if risk.primary_fact_uid is not None:
            connection.exec_driver_sql(
                "INSERT INTO guardian_risk_evidence(guardian_uid,risk_tag,fact_uid,evidence_role) "
                "VALUES (?,?,?,'SUPPORTING')",
                (guardian_uid, risk.risk_tag, risk.primary_fact_uid),
            )
    return guardian_uid, guardian_hash, effect, blocking, risks


def _insert_scout(
    connection: Any,
    snapshot: Any,
    state_uid: str,
    guardian_uid: str,
    guardian_hash: str,
    guardian_effect: str,
    facts: list[tuple[str, str, int, str]],
    thresholds: ThresholdSet,
) -> tuple[str, str, str, str, bool, list[OpportunityFinding]]:
    by_code = {code: (uid, value) for uid, code, value, _ in facts}
    opportunities = [
        OpportunityFinding(tag, f"{tag}_OBJECTIVE_THRESHOLD_MET", by_code[code][0])
        for code, tag in SCOUT_RULES
        if code in by_code and by_code[code][1] >= thresholds.entries[code]
    ]
    count = len(opportunities)
    status = "NONE" if count == 0 else "OBSERVING" if count < 3 else "ACTIVE"
    strength = "LOW" if count < 3 else "MEDIUM" if count < 6 else "HIGH"
    suppressed = guardian_effect in {"SUPPRESS", "PAUSE"}
    scout_hash = canonical_hash(
        {
            "guardian_hash": guardian_hash,
            "rule_version": ScoutService.RULE_VERSION,
            "status": status,
            "strength": strength,
            "suppressed": suppressed,
            "tags": [
                (item.opportunity_tag, item.reason_code, item.primary_fact_uid)
                for item in sorted(opportunities, key=lambda value: value.opportunity_tag)
            ],
        }
    )
    scout_uid = new_uid()
    connection.exec_driver_sql(
        "INSERT INTO scout_evaluation(scout_uid,guardian_uid,state_evaluation_uid,subject_uid,"
        "snapshot_uid,rule_version,scout_status,scout_strength,suppressed_by_guardian,"
        "guardian_effect,evaluation_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            scout_uid,
            guardian_uid,
            state_uid,
            snapshot.subject_uid,
            snapshot.snapshot_uid,
            ScoutService.RULE_VERSION,
            status,
            strength,
            int(suppressed),
            guardian_effect,
            scout_hash,
            snapshot.as_of_time,
        ),
    )
    for item in opportunities:
        connection.exec_driver_sql(
            "INSERT INTO scout_opportunity_tag(scout_uid,opportunity_tag,reason_code,"
            "primary_fact_uid) VALUES (?,?,?,?)",
            (scout_uid, item.opportunity_tag, item.reason_code, item.primary_fact_uid),
        )
        connection.exec_driver_sql(
            "INSERT INTO scout_opportunity_evidence(scout_uid,opportunity_tag,fact_uid,"
            "evidence_role) VALUES (?,?,?,'SUPPORTING')",
            (scout_uid, item.opportunity_tag, item.primary_fact_uid),
        )
    return scout_uid, scout_hash, status, strength, suppressed, opportunities


def _frozen_context(
    connection: Any,
    snapshot: Any,
    request: AnalysisCommitRequest,
    guardian_uid: str,
    guardian_effect: str,
    blocking: bool,
    risks: list[RiskFinding],
    scout_uid: str,
    scout_status: str,
    scout_strength: str,
    suppressed: bool,
    opportunities: list[OpportunityFinding],
    limitations: list[dict[str, str]],
) -> dict[str, Any]:
    confidence = confidence_from_quality(
        request.availability_state,
        str(snapshot.data_health_status),
        str(snapshot.fitness_status),
        str(snapshot.evidence_sufficiency),
        "VALID",
    ).model_dump(mode="json")
    evidence = _frozen_evidence(connection, guardian_uid, scout_uid)
    evidence_by_fact = {item.fact_uid: item for item in evidence if item.fact_uid is not None}
    return {
        "as_of_time": str(snapshot.as_of_time),
        "guardian": {
            "guardian_uid": guardian_uid,
            "effect": guardian_effect,
            "blocking": blocking,
            "risks": [
                {
                    "tag": item.risk_tag,
                    "severity": item.severity,
                    "reason_code": item.reason_code,
                    **_frozen_reason_context(
                        item.primary_fact_uid,
                        item.reason_code,
                        {"risk_tag": item.risk_tag, "severity": item.severity},
                        evidence_by_fact,
                    ),
                }
                for item in risks
            ],
        },
        "confidence": confidence,
        "scout": {
            "scout_uid": scout_uid,
            "status": scout_status,
            "strength": scout_strength,
            "suppressed_by_guardian": suppressed,
            "tags": [
                {
                    "tag": item.opportunity_tag,
                    "reason_code": item.reason_code,
                    **_frozen_reason_context(
                        item.primary_fact_uid,
                        item.reason_code,
                        {"opportunity_tag": item.opportunity_tag},
                        evidence_by_fact,
                    ),
                }
                for item in opportunities
            ],
        },
        "explanation": {
            "supporting": [
                item.model_dump(mode="json") for item in evidence if item.role == "SUPPORTING"
            ],
            "contrary": [
                item.model_dump(mode="json") for item in evidence if item.role == "CONTRARY"
            ],
        },
        "data_limitations": limitations,
    }


def _frozen_evidence(connection: Any, guardian_uid: str, scout_uid: str) -> list[EvidenceItemView]:
    rows = connection.exec_driver_sql(
        "SELECT 'risk_tag' AS tag_key,t.risk_tag AS tag,t.severity,t.reason_code,e.fact_uid,"
        "e.evidence_role,f.producer_rule_execution_uid,r.rule_key,r.rule_version "
        "FROM guardian_risk_tag t JOIN guardian_risk_evidence e "
        "ON e.guardian_uid=t.guardian_uid AND e.risk_tag=t.risk_tag "
        "JOIN fact_record f ON f.fact_uid=e.fact_uid JOIN rule_execution r "
        "ON r.rule_execution_uid=f.producer_rule_execution_uid WHERE t.guardian_uid=? "
        "UNION ALL "
        "SELECT 'opportunity_tag',t.opportunity_tag,NULL,t.reason_code,e.fact_uid,"
        "e.evidence_role,f.producer_rule_execution_uid,r.rule_key,r.rule_version "
        "FROM scout_opportunity_tag t JOIN scout_opportunity_evidence e "
        "ON e.scout_uid=t.scout_uid AND e.opportunity_tag=t.opportunity_tag "
        "JOIN fact_record f ON f.fact_uid=e.fact_uid JOIN rule_execution r "
        "ON r.rule_execution_uid=f.producer_rule_execution_uid WHERE t.scout_uid=? "
        "ORDER BY 2,6,5",
        (guardian_uid, scout_uid),
    ).all()
    result = []
    for row in rows:
        attributes = {
            str(row.tag_key): str(row.tag),
            "rule_key": str(row.rule_key),
            "rule_version": str(row.rule_version),
        }
        if row.severity is not None:
            attributes["severity"] = str(row.severity)
        result.append(
            EvidenceItemView(
                role=row.evidence_role,
                reason_code=str(row.reason_code),
                fact_uid=str(row.fact_uid),
                fact_uid_status="VALUE",
                rule_execution_uid=str(row.producer_rule_execution_uid),
                rule_execution_uid_status="VALUE",
                template_key=str(row.reason_code),
                attributes=attributes,
            )
        )
    return result


def _frozen_reason_context(
    fact_uid: str | None,
    reason_code: str,
    attributes: dict[str, str],
    evidence_by_fact: dict[str, EvidenceItemView],
) -> dict[str, Any]:
    evidence = None if fact_uid is None else evidence_by_fact.get(fact_uid)
    if evidence is not None:
        value = evidence.model_dump(mode="json")
        return {key: item for key, item in value.items() if key != "role"}
    return {
        "fact_uid": fact_uid,
        "fact_uid_status": "MISSING" if fact_uid is None else "VALUE",
        "rule_execution_uid": None,
        "rule_execution_uid_status": "MISSING",
        "template_key": reason_code,
        "attributes": attributes,
    }


def _decide_events(
    connection: Any,
    snapshot: Any,
    commit_uid: str,
    state_uid: str,
    guardian_uid: str,
    scout_uid: str,
    guardian_hash: str,
    scout_hash: str,
    guardian_effect: str,
    scout_status: str,
    suppressed: bool,
    risks: list[RiskFinding],
    opportunities: list[OpportunityFinding],
    context: dict[str, Any],
    channels: tuple[str, ...],
    generation: int,
) -> tuple[list[str], list[str]]:
    desired = {
        "GUARDIAN_RISK": guardian_effect != "ALLOW",
        "SCOUT_WATCH": scout_status == "ACTIVE" and not suppressed,
    }
    event_versions: list[str] = []
    intents: list[str] = []
    for kind, active in desired.items():
        related_key = canonical_hash({"subject_uid": snapshot.subject_uid, "event_kind": kind})
        current = connection.exec_driver_sql(
            "SELECT event_uid,event_status,version FROM current_event_projection "
            "WHERE related_key=?",
            (related_key,),
        ).one_or_none()
        if active:
            recurring = current is None or current.event_status in {"RESOLVED", "INVALIDATED"}
            event_uid = new_uid() if recurring else str(current.event_uid)
            version = 1 if recurring else int(current.version) + 1
            change_type = "CREATED" if recurring else "CHANGED"
            status = "ACTIVE"
            evidence = (
                [item.primary_fact_uid for item in risks if item.primary_fact_uid is not None]
                if kind == "GUARDIAN_RISK"
                else [item.primary_fact_uid for item in opportunities]
            )
            source_hash = guardian_hash if kind == "GUARDIAN_RISK" else scout_hash
            intent_kind = "RISK" if kind == "GUARDIAN_RISK" else "ATTENTION"
        else:
            if current is None or current.event_status not in {"CANDIDATE", "ACTIVE"}:
                continue
            event_uid = str(current.event_uid)
            version = int(current.version) + 1
            change_type = "RESOLVED"
            status = "RESOLVED"
            evidence = []
            source_hash = canonical_hash({"guardian": guardian_hash, "scout": scout_hash})
            intent_kind = "RESOLUTION"
        if active and (current is None or current.event_status in {"RESOLVED", "INVALIDATED"}):
            connection.exec_driver_sql(
                "INSERT INTO market_event(event_uid,subject_uid,event_kind,related_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (event_uid, snapshot.subject_uid, kind, related_key, snapshot.as_of_time),
            )
        version_hash = canonical_hash(
            {
                "event_uid": event_uid,
                "version": version,
                "status": status,
                "source_hash": source_hash,
            }
        )
        version_uid = new_uid()
        connection.exec_driver_sql(
            "INSERT INTO event_version(event_version_uid,event_uid,version,event_status,"
            "change_type,analysis_commit_uid,state_evaluation_uid,guardian_uid,scout_uid,"
            "version_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_uid,
                event_uid,
                version,
                status,
                change_type,
                commit_uid,
                state_uid,
                guardian_uid,
                scout_uid,
                version_hash,
                snapshot.as_of_time,
            ),
        )
        for fact_uid in sorted(set(evidence)):
            connection.exec_driver_sql(
                "INSERT INTO event_evidence(event_version_uid,fact_uid,evidence_role) "
                "VALUES (?,?,'SUPPORTING')",
                (version_uid, fact_uid),
            )
        if current is None:
            connection.exec_driver_sql(
                "INSERT INTO current_event_projection(related_key,event_uid,event_version_uid,"
                "event_status,version,updated_at) VALUES (?,?,?,?,?,?)",
                (related_key, event_uid, version_uid, status, version, snapshot.as_of_time),
            )
        else:
            connection.exec_driver_sql(
                "UPDATE current_event_projection SET event_uid=?,event_version_uid=?,"
                "event_status=?,version=?,updated_at=? WHERE related_key=?",
                (event_uid, version_uid, status, version, snapshot.as_of_time, related_key),
            )
        event_versions.append(version_uid)
        frozen = {
            **context,
            "event": {"event_uid": event_uid, "version": version, "status": status},
        }
        frozen_json = canonical_bytes(frozen).decode()
        frozen_hash = canonical_hash(frozen)
        expires_at = format_rfc3339(parse_rfc3339(str(snapshot.as_of_time)) + timedelta(minutes=5))
        for channel in channels:
            intent_uid = new_uid()
            idempotency_key = canonical_hash({"event_version_uid": version_uid, "channel": channel})
            connection.exec_driver_sql(
                "INSERT INTO notification_intent(intent_uid,event_version_uid,channel,intent_kind,"
                "idempotency_key,frozen_context_json,frozen_context_hash,created_at,expires_at,"
                "restore_generation) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    intent_uid,
                    version_uid,
                    channel,
                    intent_kind,
                    idempotency_key,
                    frozen_json,
                    frozen_hash,
                    snapshot.as_of_time,
                    expires_at,
                    generation,
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO notification_delivery_state(intent_uid,delivery_status,attempt_count,"
                "next_attempt_at,lease_owner,lease_until,last_error_code,version,updated_at) "
                "VALUES (?,'PENDING',0,?,NULL,NULL,NULL,1,?)",
                (intent_uid, snapshot.as_of_time, snapshot.as_of_time),
            )
            intents.append(intent_uid)
    return event_versions, intents
