"""Fail-closed CR-003 OFFICIAL orchestration boundary.

The orchestrator owns ordering and gates only.  It does not implement market rules or write
official business tables directly; the supplied pipeline delegates the final write to the
existing AnalysisCommitService and the post-commit delivery worker.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.official_gate import (
    OfficialExecutionPermit,
    _issue_official_execution_permit,
)


class CycleStatus(StrEnum):
    COMMITTED = "COMMITTED"
    IDEMPOTENT = "IDEMPOTENT"
    BLOCKED = "BLOCKED"


class _JournalStatus(StrEnum):
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StageStatus:
    """A read/readiness result from one startup or live-data stage."""

    ready: bool
    fitness_status: str = "FIT"
    detail: str = ""
    data: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fitness_status not in {
            "FIT",
            "FIT_WITH_LIMITATIONS",
            "UNFIT",
            "UNKNOWN",
        }:
            raise ValueError("unknown stage fitness status")
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


@dataclass(frozen=True)
class OfficialMetricOutput:
    producer_version: str
    evidence_sha256: str
    guardian_metrics: Mapping[str, int]
    scout_metrics: Mapping[str, int]
    availability_state: str
    lifecycle_state: str | None
    fitness_status: str

    def __post_init__(self) -> None:
        if not self.producer_version.strip() or len(self.evidence_sha256) != 64:
            raise ValueError("official metric output requires producer and evidence lineage")
        if self.fitness_status not in {"FIT", "FIT_WITH_LIMITATIONS", "UNFIT", "UNKNOWN"}:
            raise ValueError("unknown metric fitness status")
        if (self.availability_state == "AVAILABLE") != (self.lifecycle_state is not None):
            raise ValueError("official metric availability and lifecycle disagree")
        for metrics in (self.guardian_metrics, self.scout_metrics):
            if any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000
                for value in metrics.values()
            ):
                raise ValueError("official metrics must be integer PPM values")
        object.__setattr__(self, "guardian_metrics", MappingProxyType(dict(self.guardian_metrics)))
        object.__setattr__(self, "scout_metrics", MappingProxyType(dict(self.scout_metrics)))


@dataclass(frozen=True)
class OfficialCommitInput:
    snapshot_uid: str
    subject_uid: str
    availability_state: str
    lifecycle_state: str | None
    expected_projection_version: int
    guardian_metrics: Mapping[str, int]
    scout_metrics: Mapping[str, int]
    producer_version: str
    evidence_sha256: str
    fitness_status: str
    channels: tuple[str, ...]


@dataclass(frozen=True)
class OfficialCommitResult:
    commit_uid: str
    event_version_uids: tuple[str, ...] = ()
    intent_uids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CycleCheckpoint:
    cycle_uid: str
    cycle_key: str
    subject_uid: str
    observed_at: datetime
    phase: str
    status: str
    attempt: int
    snapshot_uid: str | None = None
    commit_uid: str | None = None
    error_code: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cycle_key.strip() or not self.subject_uid.strip():
            raise ValueError("cycle key and subject UID are required")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("cycle observation must be timezone-aware")
        if self.attempt < 1:
            raise ValueError("cycle attempt must be positive")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class CycleJournal(Protocol):
    def get(self, cycle_key: str, subject_uid: str) -> CycleCheckpoint | None: ...

    def begin(self, cycle_key: str, subject_uid: str, observed_at: datetime) -> CycleCheckpoint: ...

    def save(self, checkpoint: CycleCheckpoint) -> CycleCheckpoint: ...


class OfficialPipeline(Protocol):
    def boot(self, observed_at: datetime) -> StageStatus: ...

    def calendar_readiness(self, observed_at: datetime) -> StageStatus: ...

    def reference_readiness(self, observed_at: datetime) -> StageStatus: ...

    def provider_readiness(self, observed_at: datetime) -> StageStatus: ...

    def historical_readiness(self, observed_at: datetime) -> StageStatus: ...

    def minute_cohort_readiness(self, observed_at: datetime) -> StageStatus: ...

    def market_phase(self, observed_at: datetime) -> str: ...

    def threshold_readiness(self, observed_at: datetime) -> StageStatus: ...

    def seal_snapshot(self, subject_uid: str, cycle_key: str, observed_at: datetime) -> str: ...

    def run_metrics(
        self, snapshot_uid: str, permit: OfficialExecutionPermit
    ) -> OfficialMetricOutput: ...

    def expected_projection_version(self, subject_uid: str) -> int: ...

    def commit(self, request: OfficialCommitInput) -> OfficialCommitResult: ...

    def deliver(self) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class OfficialOrchestratorConfig:
    official_enabled: bool = False
    channels: tuple[str, ...] = ("IN_APP",)
    deliver_after_commit: bool = True
    minimum_activation_count: int = 2

    def __post_init__(self) -> None:
        channels = tuple(sorted(set(self.channels)))
        if not channels or any(channel not in {"IN_APP", "WEBHOOK"} for channel in channels):
            raise ValueError("official notification channels are invalid")
        if self.minimum_activation_count < 2:
            raise ValueError("official execution requires a Guardian/Scout threshold pair")
        object.__setattr__(self, "channels", channels)


@dataclass(frozen=True)
class OfficialCycleResult:
    status: CycleStatus
    cycle_key: str
    subject_uid: str
    phase: str
    reason: str | None = None
    commit: OfficialCommitResult | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class InMemoryCycleJournal:
    """Small deterministic journal used by unit tests and local orchestration probes."""

    def __init__(self) -> None:
        self.records: MutableMapping[tuple[str, str], CycleCheckpoint] = {}

    def get(self, cycle_key: str, subject_uid: str) -> CycleCheckpoint | None:
        return self.records.get((cycle_key, subject_uid))

    def begin(self, cycle_key: str, subject_uid: str, observed_at: datetime) -> CycleCheckpoint:
        key = (cycle_key, subject_uid)
        existing = self.records.get(key)
        if existing is not None and existing.status == _JournalStatus.COMMITTED:
            return existing
        if existing is None:
            checkpoint = CycleCheckpoint(
                new_uid(), cycle_key, subject_uid, observed_at, "SNAPSHOT", "RUNNING", 1
            )
        else:
            checkpoint = CycleCheckpoint(
                existing.cycle_uid,
                existing.cycle_key,
                existing.subject_uid,
                observed_at,
                existing.phase,
                "RUNNING",
                existing.attempt + 1,
                existing.snapshot_uid,
                existing.commit_uid,
                None,
                existing.details,
            )
        self.records[key] = checkpoint
        return checkpoint

    def save(self, checkpoint: CycleCheckpoint) -> CycleCheckpoint:
        self.records[(checkpoint.cycle_key, checkpoint.subject_uid)] = checkpoint
        return checkpoint


class SqliteCycleJournal:
    """Durable CR-003 journal backed by the single WriterQueue."""

    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def get(self, cycle_key: str, subject_uid: str) -> CycleCheckpoint | None:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT cycle_uid,cycle_key,subject_uid,observed_at,phase,status,attempt,"
                "snapshot_uid,commit_uid,error_code,detail_json FROM official_cycle_run "
                "WHERE cycle_key=? AND subject_uid=?",
                (cycle_key, subject_uid),
            ).one_or_none()
        return None if row is None else _checkpoint_from_row(row)

    def begin(self, cycle_key: str, subject_uid: str, observed_at: datetime) -> CycleCheckpoint:
        instant = format_rfc3339(observed_at)

        def command(transaction: TransactionContext) -> CycleCheckpoint:
            connection = transaction.connection
            row = connection.exec_driver_sql(
                "SELECT cycle_uid,cycle_key,subject_uid,observed_at,phase,status,attempt,"
                "snapshot_uid,commit_uid,error_code,detail_json FROM official_cycle_run "
                "WHERE cycle_key=? AND subject_uid=?",
                (cycle_key, subject_uid),
            ).one_or_none()
            if row is None:
                checkpoint = CycleCheckpoint(
                    new_uid(), cycle_key, subject_uid, observed_at, "SNAPSHOT", "RUNNING", 1
                )
                connection.exec_driver_sql(
                    "INSERT INTO official_cycle_run(cycle_uid,cycle_key,subject_uid,observed_at,"
                    "phase,status,attempt,snapshot_uid,commit_uid,error_code,detail_json,created_at,"
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)",
                    (
                        checkpoint.cycle_uid,
                        checkpoint.cycle_key,
                        checkpoint.subject_uid,
                        instant,
                        checkpoint.phase,
                        checkpoint.status,
                        checkpoint.attempt,
                        checkpoint.snapshot_uid,
                        "{}",
                        instant,
                        instant,
                    ),
                )
                return checkpoint
            existing = _checkpoint_from_row(row)
            if existing.status == _JournalStatus.COMMITTED:
                return existing
            updated = CycleCheckpoint(
                existing.cycle_uid,
                existing.cycle_key,
                existing.subject_uid,
                observed_at,
                existing.phase,
                "RUNNING",
                existing.attempt + 1,
                existing.snapshot_uid,
                existing.commit_uid,
                None,
                existing.details,
            )
            connection.exec_driver_sql(
                "UPDATE official_cycle_run SET observed_at=?,phase=?,status=?,attempt=?,"
                "error_code=NULL,updated_at=? WHERE cycle_uid=?",
                (
                    instant,
                    updated.phase,
                    updated.status,
                    updated.attempt,
                    instant,
                    updated.cycle_uid,
                ),
            )
            return updated

        return self._writer.submit(command).result()

    def save(self, checkpoint: CycleCheckpoint) -> CycleCheckpoint:
        instant = format_rfc3339(checkpoint.observed_at)

        def command(transaction: TransactionContext) -> CycleCheckpoint:
            existing = transaction.connection.exec_driver_sql(
                "SELECT detail_json FROM official_cycle_run WHERE cycle_uid=?",
                (checkpoint.cycle_uid,),
            ).one_or_none()
            if existing is None:
                raise LookupError("official cycle journal row does not exist")
            try:
                existing_details = json.loads(str(existing.detail_json))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("official cycle journal details are invalid") from error
            if not isinstance(existing_details, Mapping):
                raise ValueError("official cycle journal details are invalid")
            merged_details = dict(existing_details)
            merged_details.update(checkpoint.details)
            details = json.dumps(
                merged_details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            updated = transaction.connection.exec_driver_sql(
                "UPDATE official_cycle_run SET observed_at=?,phase=?,status=?,attempt=?,"
                "snapshot_uid=?,commit_uid=?,error_code=?,detail_json=?,updated_at=? "
                "WHERE cycle_uid=?",
                (
                    instant,
                    checkpoint.phase,
                    checkpoint.status,
                    checkpoint.attempt,
                    checkpoint.snapshot_uid,
                    checkpoint.commit_uid,
                    checkpoint.error_code,
                    details,
                    instant,
                    checkpoint.cycle_uid,
                ),
            )
            if updated.rowcount != 1:
                raise LookupError("official cycle journal row does not exist")
            return CycleCheckpoint(
                checkpoint.cycle_uid,
                checkpoint.cycle_key,
                checkpoint.subject_uid,
                checkpoint.observed_at,
                checkpoint.phase,
                checkpoint.status,
                checkpoint.attempt,
                checkpoint.snapshot_uid,
                checkpoint.commit_uid,
                checkpoint.error_code,
                merged_details,
            )

        return self._writer.submit(command).result()


class OfficialOrchestrator:
    """Run one subject cycle only after every production readiness gate passes."""

    def __init__(
        self,
        pipeline: OfficialPipeline,
        journal: CycleJournal,
        config: OfficialOrchestratorConfig,
    ) -> None:
        self._pipeline = pipeline
        self._journal = journal
        self._config = config
        self._closed = False

    def run(self, subject_uid: str, cycle_key: str, observed_at: datetime) -> OfficialCycleResult:
        if self._closed:
            raise RuntimeError("official orchestrator is closed")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("official observation must be timezone-aware")
        if not subject_uid.strip() or not cycle_key.strip():
            raise ValueError("subject UID and cycle key are required")

        existing = self._journal.get(cycle_key, subject_uid)
        if existing is not None and existing.status == _JournalStatus.COMMITTED:
            return OfficialCycleResult(
                CycleStatus.IDEMPOTENT,
                cycle_key,
                subject_uid,
                "COMPLETED",
                commit=_commit_from_checkpoint(existing),
                details=existing.details,
            )

        blocked = self._stage_gate(cycle_key, subject_uid, "BOOT", self._pipeline.boot(observed_at))
        if blocked is not None:
            return blocked
        if not self._config.official_enabled:
            return self._blocked(cycle_key, subject_uid, "BOOT", "OFFICIAL_DISABLED")

        blocked = self._stage_gate(
            cycle_key,
            subject_uid,
            "CALENDAR",
            self._pipeline.calendar_readiness(observed_at),
        )
        if blocked is not None:
            return blocked
        blocked = self._stage_gate(
            cycle_key,
            subject_uid,
            "REFERENCE",
            self._pipeline.reference_readiness(observed_at),
        )
        if blocked is not None:
            return blocked
        provider = self._pipeline.provider_readiness(observed_at)
        blocked = self._stage_gate(
            cycle_key, subject_uid, "PROVIDER", provider, "PROVIDER_NOT_READY"
        )
        if blocked is not None:
            return blocked
        historical = self._pipeline.historical_readiness(observed_at)
        blocked = self._stage_gate(
            cycle_key, subject_uid, "HISTORICAL", historical, "HISTORICAL_NOT_READY"
        )
        if blocked is not None:
            return blocked
        blocked = self._stage_gate(
            cycle_key,
            subject_uid,
            "MINUTE_COHORT",
            self._pipeline.minute_cohort_readiness(observed_at),
            "MINUTE_COHORT_NOT_READY",
        )
        if blocked is not None:
            return blocked

        phase = self._pipeline.market_phase(observed_at)
        phase_result = self._phase_gate(phase, cycle_key, subject_uid)
        if phase_result is not None:
            return phase_result

        threshold = self._pipeline.threshold_readiness(observed_at)
        activation_count = _activation_count(threshold.data)
        if not threshold.ready or activation_count < self._config.minimum_activation_count:
            return self._blocked(
                cycle_key,
                subject_uid,
                "THRESHOLD",
                "THRESHOLD_ACTIVATION_UNAVAILABLE",
                threshold.data,
            )
        if phase == "CLOSED" and threshold.data.get("final_close") is not True:
            return self._blocked(cycle_key, subject_uid, "PHASE", "FINAL_EVALUATION_NOT_PERMITTED")
        if threshold.fitness_status in {"UNFIT", "UNKNOWN"}:
            return self._blocked(
                cycle_key,
                subject_uid,
                "THRESHOLD",
                "THRESHOLD_NOT_FIT",
                threshold.data,
            )

        checkpoint = self._journal.begin(cycle_key, subject_uid, observed_at)
        if checkpoint.status == _JournalStatus.COMMITTED:
            return OfficialCycleResult(
                CycleStatus.IDEMPOTENT,
                cycle_key,
                subject_uid,
                "COMPLETED",
                commit=(_commit_from_checkpoint(checkpoint)),
                details=checkpoint.details,
            )

        try:
            snapshot_uid = checkpoint.snapshot_uid
            if snapshot_uid is None:
                snapshot_uid = self._pipeline.seal_snapshot(subject_uid, cycle_key, observed_at)
                checkpoint = self._save(
                    checkpoint,
                    phase="METRIC",
                    snapshot_uid=snapshot_uid,
                    details={
                        "threshold_activation_count": activation_count,
                        "guardian_threshold_uid": _text(
                            threshold.data.get("guardian_threshold_uid")
                        ),
                        "scout_threshold_uid": _text(threshold.data.get("scout_threshold_uid")),
                    },
                )
            permit = _issue_official_execution_permit(
                cycle_key,
                subject_uid,
                snapshot_uid,
                activation_count,
                new_uid(),
                guardian_threshold_uid=_text(threshold.data.get("guardian_threshold_uid")),
                scout_threshold_uid=_text(threshold.data.get("scout_threshold_uid")),
            )
            metric = self._pipeline.run_metrics(snapshot_uid, permit)
            if metric.fitness_status in {"UNFIT", "UNKNOWN"}:
                self._save(
                    checkpoint,
                    phase="FAILED",
                    status="FAILED",
                    snapshot_uid=snapshot_uid,
                    error_code=(
                        "METRIC_UNFIT" if metric.fitness_status == "UNFIT" else "METRIC_UNKNOWN"
                    ),
                    details={"fitness_status": metric.fitness_status},
                )
                return self._blocked(
                    cycle_key,
                    subject_uid,
                    "METRIC",
                    "METRIC_UNFIT" if metric.fitness_status == "UNFIT" else "METRIC_UNKNOWN",
                    {"fitness_status": metric.fitness_status},
                )
            if metric.availability_state != "AVAILABLE" or metric.lifecycle_state is None:
                self._save(
                    checkpoint,
                    phase="FAILED",
                    status="FAILED",
                    snapshot_uid=snapshot_uid,
                    error_code="METRIC_NOT_AVAILABLE",
                    details={
                        "availability_state": metric.availability_state,
                        "fitness_status": metric.fitness_status,
                    },
                )
                return self._blocked(
                    cycle_key,
                    subject_uid,
                    "METRIC",
                    "METRIC_NOT_AVAILABLE",
                    {"availability_state": metric.availability_state},
                )
            checkpoint = self._save(
                checkpoint,
                phase="ANALYSIS_COMMIT",
                snapshot_uid=snapshot_uid,
                details={
                    "fitness_status": metric.fitness_status,
                    "metric_producer_version": metric.producer_version,
                    "metric_evidence_sha256": metric.evidence_sha256,
                },
            )
            commit = self._pipeline.commit(
                OfficialCommitInput(
                    snapshot_uid=snapshot_uid,
                    subject_uid=subject_uid,
                    availability_state=metric.availability_state,
                    lifecycle_state=metric.lifecycle_state,
                    expected_projection_version=self._pipeline.expected_projection_version(
                        subject_uid
                    ),
                    guardian_metrics=metric.guardian_metrics,
                    scout_metrics=metric.scout_metrics,
                    producer_version=metric.producer_version,
                    evidence_sha256=metric.evidence_sha256,
                    fitness_status=metric.fitness_status,
                    channels=self._config.channels,
                )
            )
            checkpoint = self._save(
                checkpoint,
                phase="EVENT_OUTBOX",
                status="RUNNING",
                snapshot_uid=snapshot_uid,
                commit_uid=commit.commit_uid,
                details={
                    "event_version_count": len(commit.event_version_uids),
                    "intent_count": len(commit.intent_uids),
                    "event_version_uids": commit.event_version_uids,
                    "intent_uids": commit.intent_uids,
                },
            )
            if self._config.deliver_after_commit:
                self._pipeline.deliver()
            self._save(
                checkpoint,
                phase="COMPLETED",
                status="COMMITTED",
                snapshot_uid=snapshot_uid,
                commit_uid=commit.commit_uid,
                details={
                    "event_version_count": len(commit.event_version_uids),
                    "intent_count": len(commit.intent_uids),
                    "event_version_uids": commit.event_version_uids,
                    "intent_uids": commit.intent_uids,
                },
            )
            return OfficialCycleResult(
                CycleStatus.COMMITTED,
                cycle_key,
                subject_uid,
                "COMPLETED",
                commit=commit,
                details={
                    "event_version_count": len(commit.event_version_uids),
                    "intent_count": len(commit.intent_uids),
                },
            )
        except Exception as error:
            self._save(
                checkpoint,
                phase="FAILED",
                status="FAILED",
                snapshot_uid=checkpoint.snapshot_uid,
                error_code=type(error).__name__,
                details={"error": type(error).__name__},
            )
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pipeline.close()

    def _phase_gate(
        self,
        phase: str,
        cycle_key: str,
        subject_uid: str,
    ) -> OfficialCycleResult | None:
        if phase == "BREAK":
            return self._blocked(cycle_key, subject_uid, "PHASE", "MARKET_BREAK")
        if phase == "NON_TRADING_DAY":
            return self._blocked(cycle_key, subject_uid, "PHASE", "NON_TRADING_DAY")
        if phase == "PRE_OPEN":
            return self._blocked(cycle_key, subject_uid, "PHASE", "MARKET_NOT_OPEN")
        if phase not in {"CONTINUOUS_AM", "CONTINUOUS_PM", "CLOSED"}:
            return self._blocked(cycle_key, subject_uid, "PHASE", "MARKET_PHASE_UNKNOWN")
        return None

    @staticmethod
    def _stage_gate(
        cycle_key: str,
        subject_uid: str,
        name: str,
        stage: StageStatus,
        failure_reason: str | None = None,
    ) -> OfficialCycleResult | None:
        if stage.fitness_status in {"UNFIT", "UNKNOWN"}:
            reason = f"{name}_{stage.fitness_status}"
            return OfficialOrchestrator._blocked(cycle_key, subject_uid, name, reason, stage.data)
        if stage.ready:
            return None
        reason = failure_reason or f"{name}_NOT_READY"
        return OfficialOrchestrator._blocked(cycle_key, subject_uid, name, reason, stage.data)

    @staticmethod
    def _blocked(
        cycle_key: str,
        subject_uid: str,
        phase: str,
        reason: str,
        details: Mapping[str, object] | None = None,
    ) -> OfficialCycleResult:
        return OfficialCycleResult(
            CycleStatus.BLOCKED,
            cycle_key,
            subject_uid,
            phase,
            reason,
            details={} if details is None else details,
        )

    def _save(
        self,
        checkpoint: CycleCheckpoint,
        *,
        phase: str,
        status: str | None = None,
        snapshot_uid: str | None = None,
        commit_uid: str | None = None,
        error_code: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> CycleCheckpoint:
        merged_details = dict(checkpoint.details)
        if details is not None:
            merged_details.update(details)
        updated = CycleCheckpoint(
            checkpoint.cycle_uid,
            checkpoint.cycle_key,
            checkpoint.subject_uid,
            checkpoint.observed_at,
            phase,
            checkpoint.status if status is None else status,
            checkpoint.attempt,
            checkpoint.snapshot_uid if snapshot_uid is None else snapshot_uid,
            checkpoint.commit_uid if commit_uid is None else commit_uid,
            error_code,
            merged_details,
        )
        return self._journal.save(updated)


def _activation_count(data: Mapping[str, object]) -> int:
    value = data.get("activation_count", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _commit_from_checkpoint(checkpoint: CycleCheckpoint) -> OfficialCommitResult | None:
    if checkpoint.commit_uid is None:
        return None
    return OfficialCommitResult(checkpoint.commit_uid)


def _checkpoint_from_row(row: Any) -> CycleCheckpoint:
    try:
        details = json.loads(str(row.detail_json))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("official cycle journal details are invalid") from error
    if not isinstance(details, Mapping):
        raise ValueError("official cycle journal details are invalid")
    from market_monitor_persistence.values import parse_rfc3339

    return CycleCheckpoint(
        str(row.cycle_uid),
        str(row.cycle_key),
        str(row.subject_uid),
        parse_rfc3339(str(row.observed_at)),
        str(row.phase),
        str(row.status),
        int(row.attempt),
        None if row.snapshot_uid is None else str(row.snapshot_uid),
        None if row.commit_uid is None else str(row.commit_uid),
        None if row.error_code is None else str(row.error_code),
        dict(details),
    )
