"""Append-only CR-005 threshold registry and deterministic as-of resolver."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, cast

from market_monitor_persistence.artifacts import ArtifactError, ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid, parse_rfc3339, utc_now
from market_monitor_persistence.writer import TransactionContext, WriterQueue
from sqlalchemy.exc import DatabaseError

from market_monitor_analysis.canonical import canonical_hash

ThresholdFamily = Literal["GUARDIAN", "SCOUT"]
_FAMILIES = frozenset({"GUARDIAN", "SCOUT"})
_APPROVED_STATUSES = frozenset({"OWNER_APPROVED_PENDING_VALIDATION", "OWNER_APPROVED"})
_VALIDATION_KINDS = frozenset({"REPLAY", "SHADOW"})
_VALIDATION_STATUSES = frozenset({"PASS", "FAIL"})
_GUARDIAN_CODES = frozenset(
    {
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
    }
)
_SCOUT_CODES = frozenset(
    {
        "EARLY_ACTIVITY_PPM",
        "HEALTHY_BREADTH_PPM",
        "RELATIVE_STRENGTH_PPM",
        "TURNOVER_CONFIRMATION_PPM",
        "ETF_CONFIRMATION_PPM",
        "STYLE_SUPPORT_PPM",
        "LOW_CROWDING_PPM",
        "CONTINUITY_STRENGTHENING_PPM",
    }
)


class ThresholdRegistryError(RuntimeError):
    """Base error for threshold registry operations."""


class ThresholdUnavailableError(ThresholdRegistryError):
    """Raised when a requested threshold set is not eligible for resolution."""


class ThresholdActivationError(ThresholdRegistryError):
    """Raised when an activation cannot safely be appended."""


class ThresholdLineageError(ThresholdUnavailableError):
    """Raised when metric facts do not share one valid threshold version."""


@dataclass(frozen=True)
class ThresholdSet:
    uid: str
    family: ThresholdFamily
    version: str
    definition_hash: str
    entries: Mapping[str, int]
    effective_time: str | None
    owner_approval_reference: str


@dataclass(frozen=True)
class _ThresholdEvidenceReference:
    uid: str
    version: str
    definition_hash: str


@dataclass(frozen=True)
class _ProductionActivationEvidence:
    evidence_sha256: str
    replay_evidence_sha256: str
    guardian: _ThresholdEvidenceReference
    scout: _ThresholdEvidenceReference
    scope: Literal["PRODUCTION", "DEMO"]
    observed_at: str


def resolve_thresholds_for_facts(connection: Any, family: str, facts: list[Any]) -> ThresholdSet:
    resolved_family = _family(family)
    threshold_uids = {row.threshold_version_uid for row in facts}
    if None in threshold_uids or len(threshold_uids) != 1:
        raise ThresholdLineageError(
            f"{resolved_family} metric facts must use one threshold version"
        )
    return resolve_threshold_by_uid(connection, resolved_family, str(threshold_uids.pop()))


def resolve_threshold_by_uid(
    connection: Any, family: str, threshold_version_uid: str
) -> ThresholdSet:
    resolved_family = _family(family)
    row = connection.exec_driver_sql(
        "SELECT threshold_version_uid,family,version,approval_status,"
        "owner_approval_reference,definition_hash,created_at "
        "FROM threshold_version WHERE threshold_version_uid=?",
        (threshold_version_uid,),
    ).one_or_none()
    if row is None or str(row.family) != resolved_family:
        raise ThresholdLineageError(
            f"{resolved_family} metric facts reference an invalid threshold version"
        )
    return _set_from_row(connection, row, resolved_family, str(row.version), None)


class ThresholdRegistry:
    """Stores immutable threshold definitions and resolves one version per family/time."""

    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer
        self._artifacts = ArtifactStore(runtime, writer)

    def register_version(
        self,
        family: str,
        version: str,
        approval_status: str,
        owner_approval_reference: str,
        entries: Mapping[str, int],
        created_at: datetime,
    ) -> ThresholdSet:
        family = _family(family)
        normalized = _validated_entries(family, entries)
        if approval_status not in {
            "LEGACY_UNCALIBRATED",
            "OWNER_APPROVED_PENDING_VALIDATION",
            "OWNER_APPROVED",
        }:
            raise ValueError("invalid threshold approval status")
        if not version.strip() or not owner_approval_reference.strip():
            raise ValueError("threshold version and owner approval reference are required")
        effective_time = format_rfc3339(created_at)
        definition_hash = _definition_hash(family, version, normalized)

        def command(transaction: TransactionContext) -> ThresholdSet:
            existing = transaction.connection.exec_driver_sql(
                "SELECT threshold_version_uid,approval_status,owner_approval_reference,"
                "definition_hash,created_at FROM threshold_version WHERE family=? AND version=?",
                (family, version),
            ).one_or_none()
            if existing is not None:
                if (
                    str(existing.approval_status) != approval_status
                    or str(existing.owner_approval_reference) != owner_approval_reference
                    or str(existing.definition_hash) != definition_hash
                ):
                    raise ValueError(
                        "threshold version already exists with different immutable content"
                    )
                return _set_from_row(
                    transaction.connection,
                    existing,
                    family,
                    version,
                    effective_time,
                )
            uid = new_uid()
            transaction.connection.exec_driver_sql(
                "INSERT INTO threshold_version"
                "(threshold_version_uid,family,version,approval_status,owner_approval_reference,"
                "definition_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    uid,
                    family,
                    version,
                    approval_status,
                    owner_approval_reference,
                    definition_hash,
                    effective_time,
                ),
            )
            for metric_code, threshold_ppm in sorted(normalized.items()):
                transaction.connection.exec_driver_sql(
                    "INSERT INTO threshold_entry"
                    "(threshold_entry_uid,threshold_version_uid,metric_code,"
                    "threshold_ppm,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (new_uid(), uid, metric_code, threshold_ppm, effective_time),
                )
            return ThresholdSet(
                uid,
                family,
                version,
                definition_hash,
                MappingProxyType(dict(sorted(normalized.items()))),
                effective_time,
                owner_approval_reference,
            )

        return self._writer.submit(command).result()

    def resolve_explicit(self, family: str, version: str, at: datetime) -> ThresholdSet:
        family = _family(family)
        effective_time = format_rfc3339(at)
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT threshold_version_uid,approval_status,owner_approval_reference,"
                "definition_hash,created_at FROM threshold_version WHERE family=? AND version=?",
                (family, version),
            ).one_or_none()
            if row is None:
                raise ThresholdUnavailableError(f"unknown {family} threshold version {version}")
            return _set_from_row(connection, row, family, version, effective_time)

    def resolve_official(self, family: str, at: datetime) -> ThresholdSet:
        family = _family(family)
        instant = format_rfc3339(at)
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT v.threshold_version_uid,v.approval_status,v.owner_approval_reference,"
                "v.definition_hash,v.version,a.effective_at "
                "FROM threshold_activation a JOIN threshold_version v "
                "ON v.threshold_version_uid=a.threshold_version_uid "
                "WHERE a.family=? AND a.effective_at<=? "
                "ORDER BY a.effective_at DESC,a.activation_order DESC LIMIT 1",
                (family, instant),
            ).one_or_none()
            if row is None:
                raise ThresholdUnavailableError(
                    f"no active {family} threshold version at {instant}"
                )
            if str(row.approval_status) not in _APPROVED_STATUSES:
                raise ThresholdUnavailableError("active threshold version is not Owner approved")
            uid = str(row.threshold_version_uid)
            if not _latest_validations_pass(connection, uid, instant):
                raise ThresholdUnavailableError("active threshold version lacks passed validation")
            return _set_from_row(
                connection,
                row,
                family,
                str(row.version),
                str(row.effective_at),
            )

    def record_validation(
        self,
        family: str,
        version: str,
        validation_kind: str,
        validation_status: str,
        report_reference: str,
        report_sha256: str,
        observed_at: datetime,
    ) -> str:
        family = _family(family)
        if validation_kind not in _VALIDATION_KINDS:
            raise ValueError("unknown threshold validation kind")
        if validation_status not in _VALIDATION_STATUSES:
            raise ValueError("unknown threshold validation status")
        if not report_reference.strip() or len(report_sha256) != 64:
            raise ValueError("validation report reference and SHA-256 are required")
        instant = format_rfc3339(observed_at)

        def command(transaction: TransactionContext) -> str:
            version_row = transaction.connection.exec_driver_sql(
                "SELECT threshold_version_uid FROM threshold_version WHERE family=? AND version=?",
                (family, version),
            ).one_or_none()
            if version_row is None:
                raise ThresholdUnavailableError(f"unknown {family} threshold version {version}")
            artifact_exists = transaction.connection.exec_driver_sql(
                "SELECT 1 FROM artifact_object WHERE sha256=?",
                (report_sha256,),
            ).scalar_one_or_none()
            if artifact_exists is None:
                raise ValueError("threshold validation report artifact is not registered")
            existing = transaction.connection.exec_driver_sql(
                "SELECT validation_uid,validation_status,report_reference,observed_at "
                "FROM threshold_validation WHERE threshold_version_uid=? "
                "AND validation_kind=? AND report_sha256=?",
                (str(version_row.threshold_version_uid), validation_kind, report_sha256),
            ).one_or_none()
            if existing is not None:
                if (
                    str(existing.validation_status) != validation_status
                    or str(existing.report_reference) != report_reference
                    or str(existing.observed_at) != instant
                ):
                    raise ValueError("validation artifact already has immutable different content")
                return str(existing.validation_uid)
            validation_uid = new_uid()
            transaction.connection.exec_driver_sql(
                "INSERT INTO threshold_validation"
                "(validation_uid,threshold_version_uid,validation_kind,validation_status,"
                "report_reference,report_sha256,observed_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    validation_uid,
                    str(version_row.threshold_version_uid),
                    validation_kind,
                    validation_status,
                    report_reference,
                    report_sha256,
                    instant,
                    format_rfc3339(utc_now()),
                ),
            )
            return validation_uid

        return self._writer.submit(command).result()

    def record_activation_evidence(
        self, evidence_sha256: str, *, _scope: Literal["PRODUCTION", "DEMO"] = "PRODUCTION"
    ) -> str:
        """Persist one verified activation artifact; the public default is production only."""
        if not _is_sha256(evidence_sha256):
            raise ThresholdActivationError("activation evidence SHA-256 is required")
        if _scope == "PRODUCTION":
            evidence = _read_production_activation_evidence(self._artifacts, evidence_sha256)
        elif _scope == "DEMO":
            evidence = _read_demo_activation_evidence(self._artifacts, evidence_sha256)
        else:
            raise ThresholdActivationError("unknown activation evidence scope")

        def command(transaction: TransactionContext) -> str:
            guardian_uid = _evidence_threshold_uid(
                transaction.connection,
                "GUARDIAN",
                evidence.guardian,
                demo_only=_scope == "DEMO",
            )
            scout_uid = _evidence_threshold_uid(
                transaction.connection,
                "SCOUT",
                evidence.scout,
                demo_only=_scope == "DEMO",
            )
            existing = transaction.connection.exec_driver_sql(
                "SELECT acceptance_evidence_uid,replay_evidence_sha256,"
                "guardian_threshold_version_uid,scout_threshold_version_uid,"
                "evidence_scope,observed_at "
                "FROM threshold_activation_evidence "
                "WHERE evidence_sha256=?",
                (evidence.evidence_sha256,),
            ).one_or_none()
            if existing is not None:
                if (
                    str(existing.guardian_threshold_version_uid) != guardian_uid
                    or str(existing.scout_threshold_version_uid) != scout_uid
                    or str(existing.replay_evidence_sha256) != evidence.replay_evidence_sha256
                    or str(existing.evidence_scope) != evidence.scope
                    or str(existing.observed_at) != evidence.observed_at
                ):
                    raise ThresholdActivationError("activation evidence already exists differently")
                return str(existing.acceptance_evidence_uid)

            acceptance_evidence_uid = new_uid()
            transaction.connection.exec_driver_sql(
                "INSERT INTO threshold_activation_evidence("
                "acceptance_evidence_uid,evidence_sha256,replay_evidence_sha256,guardian_threshold_version_uid,"
                "scout_threshold_version_uid,evidence_scope,observed_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    acceptance_evidence_uid,
                    evidence.evidence_sha256,
                    evidence.replay_evidence_sha256,
                    guardian_uid,
                    scout_uid,
                    evidence.scope,
                    evidence.observed_at,
                    format_rfc3339(utc_now()),
                ),
            )
            return acceptance_evidence_uid

        try:
            return self._writer.submit(command).result()
        except DatabaseError as error:
            raise ThresholdActivationError(str(error)) from error

    def activate_pair(
        self,
        guardian_version: str,
        scout_version: str,
        effective_at: datetime,
        acceptance_id: str,
        *,
        acceptance_evidence_sha256: str,
        _activation_scope: Literal["PRODUCTION", "DEMO"] = "PRODUCTION",
    ) -> tuple[str, str]:
        if not acceptance_id.strip():
            raise ValueError("threshold activation acceptance ID is required")
        if not _is_sha256(acceptance_evidence_sha256):
            raise ThresholdActivationError("activation evidence SHA-256 is required")
        if _activation_scope not in {"PRODUCTION", "DEMO"}:
            raise ThresholdActivationError("unknown activation evidence scope")
        instant = format_rfc3339(effective_at)

        def command(transaction: TransactionContext) -> tuple[str, str]:
            evidence = _activation_evidence_for_pair(
                transaction,
                acceptance_evidence_sha256,
                guardian_version,
                scout_version,
                instant,
                _activation_scope,
            )
            existing = tuple(
                _activation_by_key(transaction, family, acceptance_id)
                for family in ("GUARDIAN", "SCOUT")
            )
            if any(row is not None for row in existing):
                if any(row is None for row in existing):
                    raise ThresholdActivationError("threshold activation pair is incomplete")
                assert existing[0] is not None
                assert existing[1] is not None
                expected = (guardian_version, scout_version)
                actual = tuple(str(row.version) for row in existing if row is not None)
                times = tuple(str(row.effective_at) for row in existing if row is not None)
                evidence_uids = tuple(
                    str(row.acceptance_evidence_uid) for row in existing if row is not None
                )
                if (
                    actual != expected
                    or times != (instant, instant)
                    or evidence_uids != (str(evidence.acceptance_evidence_uid),) * 2
                ):
                    raise ThresholdActivationError(
                        "threshold activation key was already used differently"
                    )
                return (str(existing[0].activation_uid), str(existing[1].activation_uid))

            guardian = _version_for_activation(
                transaction, "GUARDIAN", guardian_version, instant, acceptance_id
            )
            scout = _version_for_activation(
                transaction, "SCOUT", scout_version, instant, acceptance_id
            )
            activation_uids: list[str] = []
            for version_row in (guardian, scout):
                activation_uid = new_uid()
                transaction.connection.exec_driver_sql(
                    "INSERT INTO threshold_activation"
                    "(activation_uid,family,threshold_version_uid,effective_at,idempotency_key,"
                    "owner_approval_reference,acceptance_evidence_uid,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        activation_uid,
                        str(version_row.family),
                        str(version_row.threshold_version_uid),
                        instant,
                        acceptance_id,
                        str(version_row.owner_approval_reference),
                        str(evidence.acceptance_evidence_uid),
                        format_rfc3339(utc_now()),
                    ),
                )
                activation_uids.append(activation_uid)
            return activation_uids[0], activation_uids[1]

        try:
            return self._writer.submit(command).result()
        except DatabaseError as error:
            raise ThresholdActivationError(str(error)) from error


def _activation_by_key(
    transaction: TransactionContext, family: ThresholdFamily, acceptance_id: str
) -> Any | None:
    return transaction.connection.exec_driver_sql(
        "SELECT a.activation_uid,a.effective_at,a.acceptance_evidence_uid,v.version "
        "FROM threshold_activation a "
        "JOIN threshold_version v ON v.threshold_version_uid=a.threshold_version_uid "
        "WHERE a.family=? AND a.idempotency_key=?",
        (family, acceptance_id),
    ).one_or_none()


def _activation_evidence_for_pair(
    transaction: TransactionContext,
    evidence_sha256: str,
    guardian_version: str,
    scout_version: str,
    effective_at: str,
    evidence_scope: Literal["PRODUCTION", "DEMO"],
) -> Any:
    row = transaction.connection.exec_driver_sql(
        "SELECT e.acceptance_evidence_uid,e.observed_at,g.version AS guardian_version,"
        "s.version AS scout_version FROM threshold_activation_evidence e "
        "JOIN threshold_version g ON g.threshold_version_uid=e.guardian_threshold_version_uid "
        "JOIN threshold_version s ON s.threshold_version_uid=e.scout_threshold_version_uid "
        "WHERE e.evidence_sha256=? AND e.evidence_scope=?",
        (evidence_sha256, evidence_scope),
    ).one_or_none()
    if row is None:
        raise ThresholdActivationError("typed production activation evidence is not registered")
    if (
        str(row.guardian_version) != guardian_version
        or str(row.scout_version) != scout_version
        or str(row.observed_at) > effective_at
    ):
        raise ThresholdActivationError("typed activation evidence does not match this activation")
    return row


def _evidence_threshold_uid(
    connection: Any,
    family: ThresholdFamily,
    reference: _ThresholdEvidenceReference,
    *,
    demo_only: bool = False,
) -> str:
    row = connection.exec_driver_sql(
        "SELECT threshold_version_uid,family,version,definition_hash,owner_approval_reference "
        "FROM threshold_version "
        "WHERE threshold_version_uid=?",
        (reference.uid,),
    ).one_or_none()
    if (
        row is None
        or str(row.family) != family
        or str(row.version) != reference.version
        or str(row.definition_hash) != reference.definition_hash
        or (demo_only and str(row.owner_approval_reference) != _DEMO_OWNER_APPROVAL_REFERENCE)
        or (not demo_only and str(row.owner_approval_reference) == _DEMO_OWNER_APPROVAL_REFERENCE)
    ):
        raise ThresholdActivationError("activation evidence threshold lineage mismatch")
    return str(row.threshold_version_uid)


def _version_for_activation(
    transaction: TransactionContext,
    family: ThresholdFamily,
    version: str,
    instant: str,
    acceptance_id: str,
) -> Any:
    existing_time = transaction.connection.exec_driver_sql(
        "SELECT activation_uid FROM threshold_activation WHERE family=? AND effective_at=?",
        (family, instant),
    ).one_or_none()
    if existing_time is not None:
        raise ThresholdActivationError("threshold activation effective time is already occupied")
    row = transaction.connection.exec_driver_sql(
        "SELECT threshold_version_uid,family,version,approval_status,owner_approval_reference "
        "FROM threshold_version WHERE family=? AND version=?",
        (family, version),
    ).one_or_none()
    if row is None:
        raise ThresholdActivationError(f"unknown {family} threshold version {version}")
    if str(row.approval_status) not in _APPROVED_STATUSES:
        raise ThresholdActivationError("threshold version is not Owner approved for OFFICIAL use")
    if not _latest_validations_pass(
        transaction.connection, str(row.threshold_version_uid), instant
    ):
        raise ThresholdActivationError(
            "threshold version has not passed replay and shadow validation"
        )
    return row


def _set_from_row(
    connection: Any,
    row: Any,
    family: ThresholdFamily,
    version: str,
    effective_time: str | None,
) -> ThresholdSet:
    entries = connection.exec_driver_sql(
        "SELECT metric_code,threshold_ppm FROM threshold_entry "
        "WHERE threshold_version_uid=? ORDER BY metric_code",
        (str(row.threshold_version_uid),),
    ).all()
    values = {str(item.metric_code): int(item.threshold_ppm) for item in entries}
    try:
        normalized = _validated_entries(family, values)
    except ValueError as error:
        raise ThresholdUnavailableError(str(error)) from error
    expected_hash = _definition_hash(family, version, normalized)
    if str(row.definition_hash) != expected_hash:
        raise ThresholdUnavailableError(
            "threshold definition hash does not match immutable entries"
        )
    return ThresholdSet(
        str(row.threshold_version_uid),
        family,
        version,
        expected_hash,
        MappingProxyType(dict(sorted(normalized.items()))),
        effective_time,
        str(row.owner_approval_reference),
    )


def _latest_validations_pass(connection: Any, threshold_version_uid: str, at: str) -> bool:
    for kind in sorted(_VALIDATION_KINDS):
        status = connection.exec_driver_sql(
            "SELECT validation_status FROM threshold_validation "
            "WHERE threshold_version_uid=? AND validation_kind=? AND observed_at<=? "
            "ORDER BY observed_at DESC,validation_order DESC LIMIT 1",
            (threshold_version_uid, kind, at),
        ).scalar_one_or_none()
        if status != "PASS":
            return False
    return True


def _family(value: str) -> ThresholdFamily:
    if value not in _FAMILIES:
        raise ValueError("threshold family must be GUARDIAN or SCOUT")
    return cast(ThresholdFamily, value)


def _validated_entries(family: ThresholdFamily, entries: Mapping[str, int]) -> dict[str, int]:
    expected = _GUARDIAN_CODES if family == "GUARDIAN" else _SCOUT_CODES
    unknown = set(entries) - expected
    if unknown:
        raise ValueError(f"unknown threshold metric code: {sorted(unknown)}")
    if set(entries) != expected:
        raise ValueError("threshold version entries must be complete for its family")
    normalized: dict[str, int] = {}
    for code, value in entries.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
            raise ValueError("threshold PPM values must be integers from zero through one million")
        normalized[code] = value
    return normalized


def _definition_hash(family: ThresholdFamily, version: str, entries: Mapping[str, int]) -> str:
    return canonical_hash(
        {
            "family": family,
            "version": version,
            "entries": tuple(sorted(entries.items())),
        }
    )


def _read_production_activation_evidence(
    artifacts: ArtifactStore, evidence_sha256: str
) -> _ProductionActivationEvidence:
    try:
        with artifacts.open_verified(evidence_sha256) as stream:
            document = json.load(stream)
    except (ArtifactError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ThresholdActivationError("cannot read typed activation evidence artifact") from error
    return _parse_production_activation_evidence(artifacts, evidence_sha256, document)


def _parse_production_activation_evidence(
    artifacts: ArtifactStore, evidence_sha256: str, value: object
) -> _ProductionActivationEvidence:
    document = _activation_mapping(value, "activation evidence")
    if document.get("schema_version") != 1:
        raise ThresholdActivationError("unsupported activation evidence schema")
    if document.get("evidence_kind") != "CR005_PRODUCTION_ACTIVATION":
        raise ThresholdActivationError("invalid activation evidence kind")
    if document.get("evidence_origin") != "LIVE_SHADOW":
        raise ThresholdActivationError("fixture evidence cannot activate production thresholds")
    if document.get("validation_kind") != "SHADOW":
        raise ThresholdActivationError("production activation evidence requires Shadow validation")
    observed_at = _activation_text(document.get("observed_at"), "observed_at")
    try:
        if format_rfc3339(parse_rfc3339(observed_at)) != observed_at:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ThresholdActivationError(
            "activation evidence observed_at must be canonical RFC3339"
        ) from error

    guardian = _activation_threshold_reference(document.get("guardian_threshold"), "guardian")
    scout = _activation_threshold_reference(document.get("scout_threshold"), "scout")
    producer_version = _activation_text(
        document.get("metric_producer_version"), "metric producer version"
    )
    observations = document.get("observations")
    if not isinstance(observations, list) or len(observations) < 20:
        raise ThresholdActivationError("activation evidence requires at least 20 observations")

    observation_ids: list[str] = []
    lineages: list[tuple[str, str, str]] = []
    quality_counts = {"FIT": 0, "FIT_WITH_LIMITATIONS": 0, "UNFIT": 0}
    for index, raw in enumerate(observations):
        observation = _activation_mapping(raw, f"observation {index}")
        observation_uid = _activation_text(observation.get("observation_uid"), "observation UID")
        snapshot_uid = _activation_text(observation.get("snapshot_uid"), "snapshot UID")
        snapshot_hash = _activation_sha256(observation.get("snapshot_hash"), "snapshot hash")
        input_manifest_hash = _activation_sha256(
            observation.get("input_manifest_hash"), "input manifest hash"
        )
        _activation_sha256(observation.get("metric_evidence_sha256"), "metric evidence hash")
        if observation.get("producer_version") != producer_version:
            raise ThresholdActivationError("observation producer version mismatch")
        if observation.get("market_phase") not in _CONTINUOUS_PHASES:
            raise ThresholdActivationError(
                "activation evidence requires continuous-session observations"
            )
        quality_status = observation.get("quality_status")
        if quality_status not in quality_counts:
            raise ThresholdActivationError("invalid activation observation quality")
        observation_ids.append(observation_uid)
        lineages.append((snapshot_uid, snapshot_hash, input_manifest_hash))
        quality_counts[quality_status] += 1

    if len(set(observation_ids)) != len(observation_ids):
        raise ThresholdActivationError("activation observations must be distinct")
    if document.get("observation_identity_hash") != canonical_hash(observation_ids):
        raise ThresholdActivationError("activation observation identity lineage mismatch")
    if document.get("snapshot_input_lineage_hash") != canonical_hash(lineages):
        raise ThresholdActivationError("activation snapshot/input lineage mismatch")
    quality_distribution = _activation_mapping(
        document.get("quality_distribution"), "quality distribution"
    )
    if quality_distribution != quality_counts:
        raise ThresholdActivationError("activation quality distribution mismatch")
    _activation_sanity(document.get("sanity_result"))
    _activation_side_effect_audit(document.get("side_effect_audit"))
    replay_evidence_sha256 = _activation_replay_result(
        artifacts, document.get("replay_result"), guardian, scout
    )
    return _ProductionActivationEvidence(
        evidence_sha256, replay_evidence_sha256, guardian, scout, "PRODUCTION", observed_at
    )


def _read_demo_activation_evidence(
    artifacts: ArtifactStore, evidence_sha256: str
) -> _ProductionActivationEvidence:
    try:
        with artifacts.open_verified(evidence_sha256) as stream:
            document = _activation_mapping(json.load(stream), "demo activation evidence")
    except (ArtifactError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ThresholdActivationError("cannot read demo activation evidence artifact") from error
    if (
        document.get("schema_version") != 1
        or document.get("evidence_kind") != "M9_DEMO_ACTIVATION"
        or document.get("evidence_origin") != "DEMO_FIXTURE"
        or document.get("validation_kind") != "DEMO"
    ):
        raise ThresholdActivationError("invalid demo activation evidence")
    observed_at = _activation_text(document.get("observed_at"), "demo observed_at")
    try:
        if format_rfc3339(parse_rfc3339(observed_at)) != observed_at:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ThresholdActivationError(
            "demo evidence observed_at must be canonical RFC3339"
        ) from error
    return _ProductionActivationEvidence(
        evidence_sha256,
        evidence_sha256,
        _activation_threshold_reference(document.get("guardian_threshold"), "demo guardian"),
        _activation_threshold_reference(document.get("scout_threshold"), "demo scout"),
        "DEMO",
        observed_at,
    )


def _activation_threshold_reference(value: object, name: str) -> _ThresholdEvidenceReference:
    document = _activation_mapping(value, f"{name} threshold")
    return _ThresholdEvidenceReference(
        _activation_text(document.get("uid"), f"{name} threshold UID"),
        _activation_text(document.get("version"), f"{name} threshold version"),
        _activation_sha256(document.get("definition_hash"), f"{name} threshold definition hash"),
    )


def _activation_sanity(value: object) -> None:
    result = _activation_mapping(value, "CR-005 sanity result")
    if result.get("passed") is not True or result.get("failure_codes") != []:
        raise ThresholdActivationError("CR-005 sanity result must pass without failures")


def _activation_side_effect_audit(value: object) -> None:
    result = _activation_mapping(value, "side-effect audit")
    delta = result.get("official_business_side_effect_delta")
    if (
        result.get("passed") is not True
        or isinstance(delta, bool)
        or not isinstance(delta, int)
        or delta != 0
    ):
        raise ThresholdActivationError("activation evidence requires zero official side effects")


def _activation_replay_result(
    artifacts: ArtifactStore,
    value: object,
    guardian: _ThresholdEvidenceReference,
    scout: _ThresholdEvidenceReference,
) -> str:
    result = _activation_mapping(value, "replay result")
    if result.get("passed") is not True:
        raise ThresholdActivationError("activation evidence requires a passing replay result")
    replay_sha256 = _activation_sha256(result.get("evidence_sha256"), "replay evidence hash")
    try:
        with artifacts.open_verified(replay_sha256) as stream:
            replay = _activation_mapping(json.load(stream), "replay calibration evidence")
    except (ArtifactError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ThresholdActivationError("cannot verify replay calibration evidence") from error
    if (
        replay.get("schema_version") != 2
        or replay.get("evidence_kind") != "CR005_CALIBRATION"
        or replay.get("validation_kind") != "REPLAY"
        or replay.get("passed") is not True
        or replay.get("failure_codes") != []
        or _activation_threshold_reference(replay.get("guardian_threshold"), "replay guardian")
        != guardian
        or _activation_threshold_reference(replay.get("scout_threshold"), "replay scout") != scout
    ):
        raise ThresholdActivationError("replay calibration evidence is not production eligible")
    return replay_sha256


def _activation_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ThresholdActivationError(f"{name} must be an object")
    return value


def _activation_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThresholdActivationError(f"{name} is required")
    return value


def _activation_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _is_sha256(value):
        raise ThresholdActivationError(f"{name} must be a SHA-256")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


_CONTINUOUS_PHASES = frozenset({"CONTINUOUS_AM", "CONTINUOUS_PM"})
_DEMO_OWNER_APPROVAL_REFERENCE = "M9 demo-only fixture; never production"
