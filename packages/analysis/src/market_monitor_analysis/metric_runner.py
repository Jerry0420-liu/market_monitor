"""Run deterministic CR-004 metrics for one sealed non-OFFICIAL snapshot."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from typing import Any, Literal, cast

from market_monitor_data.clock import TradingClock
from market_monitor_persistence.artifacts import ArtifactError, ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, parse_rfc3339
from market_monitor_persistence.writer import WriterQueue

from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.facts import FactExecutor
from market_monitor_analysis.guardian import guardian_threshold_matches
from market_monitor_analysis.market_metrics import (
    ContextMetric,
    ContinuityObservation,
    HistoricalSameClockWindow,
    MarketMetricProducer,
    MetricInput,
    MetricMember,
    MetricQuality,
)
from market_monitor_analysis.official_gate import (
    OfficialExecutionPermit,
    is_official_execution_permit,
)
from market_monitor_analysis.scout import scout_threshold_matches
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_analysis.thresholds import ThresholdRegistry, ThresholdUnavailableError

_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")
_QUOTE_STATUSES = frozenset({"VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"})
_REQUIRED_CAPABILITIES = frozenset({"QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"})
_SQL_READ_BATCH_SIZE = 500


def _rows_for_identifiers(connection: Any, statement: str, identifiers: Sequence[str]) -> list[Any]:
    rows: list[Any] = []
    for offset in range(0, len(identifiers), _SQL_READ_BATCH_SIZE):
        batch = identifiers[offset : offset + _SQL_READ_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(
            connection.exec_driver_sql(
                statement.format(placeholders=placeholders), tuple(batch)
            ).all()
        )
    return rows


class MetricRunnerError(RuntimeError):
    """Base error for sealed CR-004 metric execution."""


class MetricRunnerDispositionError(MetricRunnerError):
    """Raised when a caller attempts to use the Shadow/Replay runner for OFFICIAL work."""


class MetricRunInProgressError(MetricRunnerError):
    """Raised instead of queuing a stale full metric evaluation."""


class MetricInputLineageError(MetricRunnerError):
    """Raised when a sealed snapshot cannot prove canonical CR-004 inputs."""


@dataclass(frozen=True)
class MetricRunResult:
    snapshot_uid: str
    producer_version: str
    evidence_sha256: str
    guardian_fact_uids: tuple[str, ...]
    scout_fact_uids: tuple[str, ...]
    guardian_threshold_matches: tuple[str, ...]
    scout_threshold_matches: tuple[str, ...]
    quality: MetricQuality
    elapsed_ms: int
    evaluation_time: datetime
    metric_evaluation_ms: int = 0
    threshold_evaluation_ms: int = 0
    evidence_persistence_ms: int = 0
    guardian_metrics: Mapping[str, int] = field(default_factory=dict)
    scout_metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Snapshot:
    snapshot_uid: str
    subject_uid: str
    manifest_uid: str
    bundle_uid: str
    disposition: str
    status: str
    canonical_hash: str
    as_of_time: datetime
    data_health_status: str
    fitness_status: str


@dataclass(frozen=True)
class _Bar:
    instrument_uid: str
    interval: str
    source_time: datetime
    close: Decimal
    amount: Decimal
    epoch_uid: str


class MetricRunner:
    """Persist CR-004 facts through the explicit Shadow/Replay or OFFICIAL boundary."""

    def __init__(
        self, runtime: DatabaseRuntime, writer: WriterQueue, artifacts: ArtifactStore
    ) -> None:
        self._runtime = runtime
        self._writer = writer
        self._artifacts = artifacts
        # ponytail: process-wide full-evaluation lock.
        # CR-003 can only narrow it after cadence evidence.
        self._lock = Lock()

    def run(
        self,
        snapshot_uid: str,
        *,
        guardian_threshold_version: str,
        scout_threshold_version: str,
        metric_producer_version: str = MarketMetricProducer.RULE_VERSION,
    ) -> MetricRunResult:
        """Run the non-OFFICIAL Shadow or Historical Replay path."""
        return self._run_internal(
            snapshot_uid,
            guardian_threshold_version=guardian_threshold_version,
            scout_threshold_version=scout_threshold_version,
            metric_producer_version=metric_producer_version,
            official_permit=None,
        )

    def run_official(
        self,
        snapshot_uid: str,
        permit: OfficialExecutionPermit,
        *,
        metric_producer_version: str = MarketMetricProducer.RULE_VERSION,
    ) -> MetricRunResult:
        """Run CR-004 only for an orchestrator-issued, sealed OFFICIAL snapshot."""
        if not is_official_execution_permit(permit):
            raise MetricRunnerDispositionError("OFFICIAL metrics require an orchestrator permit")
        return self._run_internal(
            snapshot_uid,
            guardian_threshold_version=None,
            scout_threshold_version=None,
            metric_producer_version=metric_producer_version,
            official_permit=permit,
        )

    def _run_internal(
        self,
        snapshot_uid: str,
        *,
        guardian_threshold_version: str | None,
        scout_threshold_version: str | None,
        metric_producer_version: str,
        official_permit: OfficialExecutionPermit | None,
    ) -> MetricRunResult:
        if not self._lock.acquire(blocking=False):
            raise MetricRunInProgressError(snapshot_uid)
        started = monotonic()
        try:
            snapshot = self._snapshot(snapshot_uid)
            if snapshot.status != "SEALED":
                raise MetricInputLineageError("CR-004 metrics require a sealed snapshot")
            if official_permit is None:
                if snapshot.disposition not in {"SHADOW", "HISTORICAL_REPLAY"}:
                    raise MetricRunnerDispositionError("CR-004 metric runner is non-OFFICIAL")
            elif snapshot.disposition != "OFFICIAL":
                raise MetricRunnerDispositionError(
                    "production metric entry requires an OFFICIAL snapshot"
                )
            elif (
                official_permit.snapshot_uid != snapshot.snapshot_uid
                or official_permit.subject_uid != snapshot.subject_uid
            ):
                raise MetricRunnerDispositionError(
                    "OFFICIAL permit does not match the sealed snapshot lineage"
                )

            builder = SnapshotBuilder(self._runtime, self._writer, self._artifacts)
            manifest = builder.replay_manifest(snapshot.manifest_uid)
            manifest_schema_version = SnapshotBuilder.manifest_schema_version(manifest)
            if manifest_schema_version not in {2, 3, 4, 5}:
                raise MetricInputLineageError(
                    "CR-004 metrics require an InputManifest schema version 2, 3, 4, or 5"
                )

            registry = ThresholdRegistry(self._runtime, self._writer)
            try:
                if official_permit is None:
                    if guardian_threshold_version is None or scout_threshold_version is None:
                        raise MetricInputLineageError(
                            "explicit Shadow/Replay thresholds are required"
                        )
                    guardian_thresholds = registry.resolve_explicit(
                        "GUARDIAN", guardian_threshold_version, snapshot.as_of_time
                    )
                    scout_thresholds = registry.resolve_explicit(
                        "SCOUT", scout_threshold_version, snapshot.as_of_time
                    )
                else:
                    if (
                        official_permit.threshold_activation_count < 2
                        or not official_permit.guardian_threshold_uid
                        or not official_permit.scout_threshold_uid
                    ):
                        raise MetricRunnerDispositionError(
                            "OFFICIAL permit lacks an active threshold pair"
                        )
                    guardian_thresholds = registry.resolve_official("GUARDIAN", snapshot.as_of_time)
                    scout_thresholds = registry.resolve_official("SCOUT", snapshot.as_of_time)
                    if (
                        guardian_thresholds.uid != official_permit.guardian_threshold_uid
                        or scout_thresholds.uid != official_permit.scout_threshold_uid
                    ):
                        raise MetricRunnerDispositionError(
                            "OFFICIAL threshold activation changed after readiness"
                        )
            except ThresholdUnavailableError as error:
                if official_permit is not None:
                    raise MetricRunnerDispositionError(
                        "OFFICIAL threshold activation is unavailable"
                    ) from error
                raise
            metric_started = monotonic()
            metric_input, lineage = self._metric_input(snapshot, manifest)
            producer = MarketMetricProducer(metric_producer_version)
            base_output = producer.evaluate(metric_input)
            if base_output.rule_version == MarketMetricProducer.RULE_VERSION:
                observations = self._continuity_observations(snapshot, base_output)
                metric_input = replace(
                    metric_input,
                    continuity_observations=observations,
                )
                lineage["continuity_observation_uids"] = tuple(
                    item.observation_uid for item in observations
                )
            output = producer.evaluate(metric_input)
            metric_evaluation_ms = int((monotonic() - metric_started) * 1000)
            threshold_started = monotonic()
            guardian_matches = guardian_threshold_matches(
                output.guardian_metrics, guardian_thresholds.entries
            )
            scout_matches = scout_threshold_matches(output.scout_metrics, scout_thresholds.entries)
            threshold_evaluation_ms = int((monotonic() - threshold_started) * 1000)
            evidence_started = monotonic()
            evidence = {
                "schema_version": 2,
                **self._evidence_binding(snapshot),
                "manifest_schema_version": manifest_schema_version,
                "producer_version": output.rule_version,
                "input_lineage": lineage,
                "guardian_threshold": {
                    "uid": guardian_thresholds.uid,
                    "version": guardian_thresholds.version,
                    "definition_hash": guardian_thresholds.definition_hash,
                },
                "scout_threshold": {
                    "uid": scout_thresholds.uid,
                    "version": scout_thresholds.version,
                    "definition_hash": scout_thresholds.definition_hash,
                },
                "guardian_threshold_matches": guardian_matches,
                "scout_threshold_matches": scout_matches,
                "metric_output": dict(output.evidence),
                "metric_output_sha256": output.evidence_sha256,
            }
            if official_permit is not None:
                evidence["official_cycle_key"] = official_permit.cycle_key
            artifact = self._artifacts.put_bytes(
                canonical_bytes(evidence),
                "application/vnd.market-monitor.cr004-metric-evidence+json",
            )
            self._artifacts.register(artifact)
            executor = FactExecutor(self._runtime, self._writer)
            guardian_facts = executor.record_metrics(
                snapshot.snapshot_uid,
                "CR004_GUARDIAN_METRICS",
                output.rule_version,
                output.guardian_metrics,
                threshold_version_uid=guardian_thresholds.uid,
                evidence_sha256=artifact.sha256,
            )
            scout_facts = executor.record_scout_metrics(
                snapshot.snapshot_uid,
                "CR004_SCOUT_METRICS",
                output.rule_version,
                output.scout_metrics,
                threshold_version_uid=scout_thresholds.uid,
                evidence_sha256=artifact.sha256,
            )
            evidence_persistence_ms = int((monotonic() - evidence_started) * 1000)
            return MetricRunResult(
                snapshot.snapshot_uid,
                output.rule_version,
                artifact.sha256,
                tuple(fact.fact_uid for fact in guardian_facts),
                tuple(fact.fact_uid for fact in scout_facts),
                guardian_matches,
                scout_matches,
                output.quality,
                int((monotonic() - started) * 1000),
                snapshot.as_of_time,
                metric_evaluation_ms,
                threshold_evaluation_ms,
                evidence_persistence_ms,
                output.guardian_metrics,
                output.scout_metrics,
            )
        finally:
            self._lock.release()

    def _snapshot(self, snapshot_uid: str) -> _Snapshot:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT s.snapshot_uid,s.subject_uid,s.manifest_uid,s.bundle_uid,"
                "s.evaluation_disposition,s.snapshot_status,s.canonical_hash,s.as_of_time,"
                "q.data_health_status,q.fitness_status "
                "FROM evaluation_snapshot s "
                "JOIN quality_context q ON q.quality_context_uid=s.quality_context_uid "
                "WHERE s.snapshot_uid=?",
                (snapshot_uid,),
            ).one_or_none()
        if row is None:
            raise MetricInputLineageError(f"unknown snapshot: {snapshot_uid}")
        return _Snapshot(
            str(row.snapshot_uid),
            str(row.subject_uid),
            str(row.manifest_uid),
            str(row.bundle_uid),
            str(row.evaluation_disposition),
            str(row.snapshot_status),
            str(row.canonical_hash),
            parse_rfc3339(str(row.as_of_time)),
            str(row.data_health_status),
            str(row.fitness_status),
        )

    def _evidence_binding(self, snapshot: _Snapshot) -> dict[str, object]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT m.canonical_hash AS manifest_hash,m.artifact_sha256,"
                "b.canonical_hash AS bundle_hash "
                "FROM input_manifest m JOIN reference_version_bundle b "
                "ON b.bundle_uid=? WHERE m.manifest_uid=?",
                (snapshot.bundle_uid, snapshot.manifest_uid),
            ).one_or_none()
            reference_entries = [
                [str(item.entity_kind), str(item.entity_uid), str(item.version_uid)]
                for item in connection.exec_driver_sql(
                    "SELECT entity_kind,entity_uid,version_uid FROM reference_version_entry "
                    "WHERE bundle_uid=? ORDER BY entity_kind,entity_uid,version_uid",
                    (snapshot.bundle_uid,),
                ).all()
            ]
            source_epoch_uids = [
                str(item)
                for item in connection.exec_driver_sql(
                    "SELECT DISTINCT epoch_uid FROM capability_snapshot "
                    "WHERE snapshot_uid=? ORDER BY epoch_uid",
                    (snapshot.snapshot_uid,),
                ).scalars()
            ]
        if row is None:
            raise MetricInputLineageError("snapshot lacks manifest or reference bundle lineage")
        return {
            "snapshot_uid": snapshot.snapshot_uid,
            "snapshot_hash": snapshot.canonical_hash,
            "subject_uid": snapshot.subject_uid,
            "manifest_uid": snapshot.manifest_uid,
            "manifest_hash": str(row.manifest_hash),
            "manifest_artifact_sha256": str(row.artifact_sha256),
            "bundle_uid": snapshot.bundle_uid,
            "bundle_hash": str(row.bundle_hash),
            "reference_entries": reference_entries,
            "source_epoch_uids": source_epoch_uids,
        }

    def _metric_input(
        self, snapshot: _Snapshot, manifest: Mapping[str, Any]
    ) -> tuple[MetricInput, dict[str, object]]:
        quote_uids = _identifier_list(manifest.get("quote_uids"), "quote_uids")
        context_quote_uids = _identifier_list(
            manifest.get("context_quote_uids", []), "context_quote_uids"
        )
        shadow_round_uid = manifest.get("shadow_round_uid")
        if shadow_round_uid is not None and (
            not isinstance(shadow_round_uid, str) or not shadow_round_uid
        ):
            raise MetricInputLineageError("manifest shadow round UID is invalid")
        if set(quote_uids) & set(context_quote_uids):
            raise MetricInputLineageError("Primary and context quote lineage must not overlap")
        bar_uids = _identifier_list(manifest.get("bar_uids"), "bar_uids")
        if not bar_uids:
            raise MetricInputLineageError("CR-004 metrics require canonical bars")
        statuses = _primary_statuses(manifest)
        windows = _historical_dates(manifest)
        membership = self._membership(snapshot)
        sector_uid = str(membership.sector_uid)
        membership_uid = str(membership.membership_version_uid)
        sector_members = self._sector_members(membership_uid)
        if not sector_members or not set(sector_members).issubset(statuses):
            raise MetricInputLineageError(
                "sector membership must belong to Primary Universe inventory"
            )
        exchange = self._calendar_exchange(snapshot.bundle_uid, snapshot.as_of_time)
        clock = TradingClock(self._runtime, self._writer)
        phase = clock.phase_at(exchange, snapshot.as_of_time)
        is_final_trading_snapshot = (
            phase == "CLOSED"
            and clock.final_continuous_close(exchange, snapshot.as_of_time) == snapshot.as_of_time
        )
        expected_same_clock_dates = clock.previous_valid_trading_dates(
            exchange,
            snapshot.as_of_time.astimezone(_CHINA).date().isoformat(),
            5,
        )
        if windows != expected_same_clock_dates:
            raise MetricInputLineageError(
                "historical windows must be the previous valid trading days"
            )
        quotes, quote_epoch = self._quotes(quote_uids, statuses)
        context_quotes = self._context_quotes(context_quote_uids)
        bars, bar_epoch = self._bars(bar_uids)
        if quote_epoch is not None and bar_epoch != quote_epoch:
            raise MetricInputLineageError("quote and bar input source epochs differ")

        minute_by_instrument: dict[str, tuple[_Bar, ...]] = {}
        daily_by_instrument: dict[str, tuple[_Bar, ...]] = {}
        grouped: dict[tuple[str, str], list[_Bar]] = defaultdict(list)
        for bar in bars:
            grouped[(bar.instrument_uid, bar.interval)].append(bar)
        for instrument_uid in statuses:
            minute_by_instrument[instrument_uid] = tuple(
                sorted(grouped[(instrument_uid, "1m")], key=lambda item: item.source_time)
            )
            daily_by_instrument[instrument_uid] = tuple(
                sorted(grouped[(instrument_uid, "1d")], key=lambda item: item.source_time)
            )

        current_session = clock.continuous_session_at(exchange, snapshot.as_of_time)
        current_tails = {
            instrument_uid: _complete_current_tail(
                minute_by_instrument[instrument_uid],
                snapshot.as_of_time,
                clock,
                exchange,
                current_session,
            )
            for instrument_uid in statuses
        }
        members: list[MetricMember] = []
        complete_minute_members = 0
        for instrument_uid, (status, quote_uid) in sorted(statuses.items()):
            tail = current_tails[instrument_uid]
            price, pre_close = quotes.get(instrument_uid, (None, None))
            if status == "VALID" and tail is not None:
                complete_minute_members += 1
            members.append(
                MetricMember(
                    instrument_uid,
                    cast(Literal["VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"], status),
                    price,
                    pre_close,
                    None if tail is None or len(tail) < 5 else tail[-5].close,
                    None if tail is None or len(tail) < 10 else tail[-10].close,
                    None if tail is None or len(tail) < 15 else tail[-15].close,
                    None
                    if tail is None or len(tail) < 5
                    else sum((item.amount for item in tail[-5:]), Decimal("0")),
                    None
                    if tail is None or len(tail) < 15
                    else sum((item.amount for item in tail), Decimal("0")),
                    None
                    if tail is None or len(tail) < 10
                    else sum((item.amount for item in tail[-10:-5]), Decimal("0")),
                    tuple(
                        item.amount
                        for item in daily_by_instrument[instrument_uid]
                        if item.source_time <= snapshot.as_of_time and item.amount > 0
                    )[-20:],
                    status == "SUSPENDED",
                )
            )

        minute_integrity = complete_minute_members > 0
        active_sector_members = [
            instrument_uid
            for instrument_uid in sector_members
            if statuses[instrument_uid][0] != "SUSPENDED"
        ]
        historical_amounts: dict[str, dict[str, Decimal]] = {}
        for instrument_uid in active_sector_members:
            tail = current_tails[instrument_uid]
            if tail is None:
                historical_amounts[instrument_uid] = {}
                continue
            historical_amounts[instrument_uid] = {
                trading_date: amount
                for trading_date in windows
                if (
                    amount := _same_clock_amount(
                        minute_by_instrument[instrument_uid], trading_date, tail[-1].source_time
                    )
                )
                is not None
            }
        observations = sum(
            1
            for instrument_uid in active_sector_members
            for trading_date in windows
            if trading_date in historical_amounts.get(instrument_uid, {})
        )
        expected_observations = len(active_sector_members) * len(windows)
        historical_coverage = (
            Decimal("0")
            if expected_observations == 0
            else Decimal(observations) / Decimal(expected_observations)
        )
        same_clock_windows = tuple(
            HistoricalSameClockWindow(
                trading_date,
                tuple(
                    sorted(
                        (instrument_uid, historical_amounts[instrument_uid][trading_date])
                        for instrument_uid in active_sector_members
                        if trading_date in historical_amounts.get(instrument_uid, {})
                    )
                ),
            )
            for trading_date in windows
            if any(trading_date in amounts for amounts in historical_amounts.values())
        )
        provider_capability_fit = (
            snapshot.fitness_status != "UNFIT"
            and minute_integrity
            and self._capability_fit(snapshot.snapshot_uid)
        )
        provider_capability_limited = provider_capability_fit and (
            snapshot.data_health_status != "HEALTHY"
            or snapshot.fitness_status == "FIT_WITH_LIMITATIONS"
        )
        (
            etf_context,
            style_context,
            context_fit,
            context_lineage,
        ) = self._context_metrics(
            snapshot,
            sector_uid,
            context_quotes,
            grouped,
            clock,
            exchange,
            current_session,
        )
        metric_input = MetricInput(
            sector_uid=sector_uid,
            sector_member_uids=sector_members,
            primary_members=tuple(members),
            same_clock_windows=same_clock_windows,
            historical_coverage=historical_coverage,
            provider_capability_fit=provider_capability_fit,
            provider_capability_limited=provider_capability_limited,
            trading_date=snapshot.as_of_time.astimezone(_CHINA).date().isoformat(),
            market_phase=phase,
            has_complete_minute=minute_integrity,
            is_final_trading_snapshot=is_final_trading_snapshot,
            etf_context=etf_context,
            style_context=style_context,
            context_fit=context_fit,
        )
        status_counts = dict(sorted(Counter(status for status, _ in statuses.values()).items()))
        lineage: dict[str, object] = {
            "source_epoch_uid": bar_epoch if bar_epoch is not None else quote_epoch,
            "shadow_round_uid": shadow_round_uid,
            "membership_version_uid": membership_uid,
            "membership_kind": str(membership.membership_kind),
            "block_version_uid": str(membership.block_version_uid),
            "block_name": str(membership.block_name),
            "block_type": int(membership.block_type),
            "primary_instrument_count": len(statuses),
            "primary_quote_status_counts": status_counts,
            "complete_minute_member_count": complete_minute_members,
            "complete_minute_member_coverage_ppm": (
                0 if not statuses else (complete_minute_members * 1_000_000) // len(statuses)
            ),
            "bar_count": len(bar_uids),
            "context_mapping": context_lineage,
            "same_clock_trading_dates": windows,
            "historical_observations": observations,
            "historical_expected_observations": expected_observations,
            "historical_coverage": str(historical_coverage),
            "market_phase": phase,
            "provider_capability_fit": provider_capability_fit,
            "provider_capability_limited": provider_capability_limited,
        }
        if manifest.get("schema_version") == 5:
            lineage["realtime_minute_cohort"] = manifest["realtime_minute_cohort"]
        return metric_input, lineage

    def _continuity_observations(
        self,
        snapshot: _Snapshot,
        output: Any,
    ) -> tuple[ContinuityObservation, ...]:
        exchange = self._calendar_exchange(snapshot.bundle_uid, snapshot.as_of_time)
        clock = TradingClock(self._runtime, self._writer)
        current = _continuity_observation(
            snapshot.snapshot_uid,
            snapshot.as_of_time,
            snapshot.as_of_time.astimezone(_CHINA).date().isoformat(),
            clock.market_minute_index(exchange, snapshot.as_of_time),
            output.quality.fitness_status,
            output.evidence.get("base_facts"),
        )
        if current is None:
            return ()
        previous = self._prior_continuity_observations(
            snapshot,
            output.rule_version,
            exchange,
            clock,
        )
        if len(previous) != 2:
            return ()
        observations = tuple(
            sorted(
                (*previous, current),
                key=lambda item: (
                    item.market_minute_index,
                    item.observed_at,
                    item.observation_uid,
                ),
            )
        )
        if (
            len({item.observation_uid for item in observations}) != 3
            or clock.market_minutes_between(
                exchange,
                observations[0].observed_at,
                observations[-1].observed_at,
            )
            < 10
        ):
            return ()
        return observations

    def _prior_continuity_observations(
        self,
        snapshot: _Snapshot,
        producer_version: str,
        exchange: str,
        clock: TradingClock,
    ) -> tuple[ContinuityObservation, ...]:
        local_start = snapshot.as_of_time.astimezone(_CHINA).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        local_end = local_start + timedelta(days=1)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT s.snapshot_uid,s.as_of_time,s.canonical_hash,r.output_hash,"
                "current_bundle.canonical_hash AS bundle_hash "
                "FROM evaluation_snapshot s "
                "JOIN reference_version_bundle prior_bundle "
                "ON prior_bundle.bundle_uid=s.bundle_uid "
                "JOIN reference_version_bundle current_bundle "
                "ON current_bundle.bundle_uid=? "
                "JOIN rule_execution r ON r.snapshot_uid=s.snapshot_uid "
                "WHERE s.subject_uid=? AND s.snapshot_status='SEALED' "
                "AND s.evaluation_disposition IN ('SHADOW','HISTORICAL_REPLAY') "
                "AND s.as_of_time>=? AND s.as_of_time<? AND s.as_of_time<? "
                "AND prior_bundle.canonical_hash=current_bundle.canonical_hash "
                "AND r.rule_key='CR004_GUARDIAN_METRICS' AND r.rule_version=? "
                "AND r.output_hash IS NOT NULL "
                "ORDER BY s.as_of_time DESC,s.snapshot_uid DESC LIMIT 2",
                (
                    snapshot.bundle_uid,
                    snapshot.subject_uid,
                    format_rfc3339(local_start),
                    format_rfc3339(local_end),
                    format_rfc3339(snapshot.as_of_time),
                    producer_version,
                ),
            ).all()
        if len(rows) != 2:
            return ()
        observations: list[ContinuityObservation] = []
        for row in rows:
            observation = self._artifact_continuity_observation(
                str(row.output_hash),
                str(row.snapshot_uid),
                parse_rfc3339(str(row.as_of_time)),
                str(row.canonical_hash),
                snapshot.subject_uid,
                str(row.bundle_hash),
                producer_version,
                exchange,
                clock,
            )
            if observation is None:
                return ()
            observations.append(observation)
        return tuple(sorted(observations, key=lambda item: item.observed_at))

    def _artifact_continuity_observation(
        self,
        evidence_sha256: str,
        snapshot_uid: str,
        observed_at: datetime,
        snapshot_hash: str,
        subject_uid: str,
        bundle_hash: str,
        producer_version: str,
        exchange: str,
        clock: TradingClock,
    ) -> ContinuityObservation | None:
        try:
            with self._artifacts.open_verified(evidence_sha256) as stream:
                evidence = json.load(stream)
        except ArtifactError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        if not isinstance(evidence, Mapping):
            return None
        metric_output = evidence.get("metric_output")
        if (
            evidence.get("schema_version") != 2
            or evidence.get("snapshot_uid") != snapshot_uid
            or evidence.get("snapshot_hash") != snapshot_hash
            or evidence.get("subject_uid") != subject_uid
            or evidence.get("bundle_hash") != bundle_hash
            or evidence.get("producer_version") != producer_version
            or not isinstance(metric_output, Mapping)
            or metric_output.get("rule_version") != producer_version
            or evidence.get("metric_output_sha256") != canonical_hash(metric_output)
        ):
            return None
        quality = metric_output.get("quality")
        return _continuity_observation(
            snapshot_uid,
            observed_at,
            observed_at.astimezone(_CHINA).date().isoformat(),
            clock.market_minute_index(exchange, observed_at),
            quality.get("fitness_status") if isinstance(quality, Mapping) else None,
            metric_output.get("base_facts"),
        )

    def _capability_fit(self, snapshot_uid: str) -> bool:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT capability,health_status,fitness_status FROM capability_snapshot "
                "WHERE snapshot_uid=? ORDER BY capability",
                (snapshot_uid,),
            ).all()
        states = {
            str(row.capability): (str(row.health_status), str(row.fitness_status)) for row in rows
        }
        if all(
            states.get(capability) in {("HEALTHY", "FIT"), ("DEGRADED", "FIT_WITH_LIMITATIONS")}
            for capability in _REQUIRED_CAPABILITIES
        ):
            return True
        return all(
            states.get(capability) == ("HEALTHY", "FIT") for capability in _REQUIRED_CAPABILITIES
        )

    def _membership(self, snapshot: _Snapshot) -> Any:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT a.sector_uid,m.membership_version_uid,m.trading_date,"
                "src.membership_kind,src.block_name,src.block_type,src.block_version_uid "
                "FROM analysis_subject a "
                "JOIN reference_version_entry e ON e.bundle_uid=? "
                "AND e.entity_kind='MEMBERSHIP' AND e.entity_uid=a.sector_uid "
                "JOIN sector_membership_version m ON m.membership_version_uid=e.version_uid "
                "JOIN tdx_sector_membership_source src "
                "ON src.membership_version_uid=m.membership_version_uid "
                "WHERE a.subject_uid=? AND a.subject_kind='SECTOR' "
                "AND m.sector_uid=a.sector_uid ORDER BY m.membership_version_uid",
                (snapshot.bundle_uid, snapshot.subject_uid),
            ).all()
            sector_reference = connection.exec_driver_sql(
                "SELECT count(*) FROM reference_version_entry "
                "WHERE bundle_uid=? AND entity_kind='SECTOR' "
                "AND entity_uid=(SELECT sector_uid FROM analysis_subject WHERE subject_uid=?)",
                (snapshot.bundle_uid, snapshot.subject_uid),
            ).scalar_one()
        if len(row) != 1 or int(sector_reference) != 1:
            raise MetricInputLineageError(
                "snapshot requires one versioned sector membership reference"
            )
        value = row[0]
        if str(value.trading_date) != snapshot.as_of_time.astimezone(_CHINA).date().isoformat():
            raise MetricInputLineageError(
                "membership trading date does not match metric evaluation time"
            )
        return value

    def _sector_members(self, membership_uid: str) -> tuple[str, ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT instrument_uid FROM sector_membership "
                "WHERE membership_version_uid=? ORDER BY instrument_uid",
                (membership_uid,),
            ).all()
        return tuple(str(row.instrument_uid) for row in rows)

    def _calendar_exchange(self, bundle_uid: str, as_of: datetime) -> str:
        trading_date = as_of.astimezone(_CHINA).date().isoformat()
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT entity_uid FROM reference_version_entry "
                "WHERE bundle_uid=? AND entity_kind='CALENDAR' AND version_uid=? "
                "ORDER BY entity_uid",
                (bundle_uid, trading_date),
            ).all()
        if not rows:
            raise MetricInputLineageError("snapshot requires TradingClock calendar lineage")
        return str(rows[0].entity_uid)

    def _quotes(
        self,
        quote_uids: Sequence[str],
        statuses: Mapping[str, tuple[str, str | None]],
    ) -> tuple[dict[str, tuple[Decimal, Decimal]], str | None]:
        with self._runtime.read_connection() as connection:
            rows = _rows_for_identifiers(
                connection,
                "SELECT q.quote_uid,l.instrument_uid,l.epoch_uid,q.price_status,q.price_scaled,"
                "q.price_scale,d.pre_close_scaled,d.price_scale AS detail_price_scale,"
                "d.quote_status "
                "FROM market_quote q JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
                "LEFT JOIN tdx_quote_detail d ON d.quote_uid=q.quote_uid "
                "WHERE q.quote_uid IN ({placeholders}) ORDER BY q.quote_uid",
                quote_uids,
            )
        if len(rows) != len(quote_uids):
            raise MetricInputLineageError("manifest references an unknown canonical quote")
        by_uid = {str(row.quote_uid): row for row in rows}
        expected_uids = {quote_uid for status, quote_uid in statuses.values() if status == "VALID"}
        if set(quote_uids) != expected_uids:
            raise MetricInputLineageError(
                "manifest valid quote inventory does not match primary statuses"
            )
        values: dict[str, tuple[Decimal, Decimal]] = {}
        epochs: set[str] = set()
        for instrument_uid, (status, quote_uid) in statuses.items():
            if status != "VALID":
                continue
            row = by_uid.get(cast(str, quote_uid))
            if (
                row is None
                or str(row.instrument_uid) != instrument_uid
                or str(row.price_status) != "VALUE"
                or row.price_scaled is None
                or row.pre_close_scaled is None
                or str(row.quote_status) != "VALUE"
            ):
                raise MetricInputLineageError("VALID primary quote lacks canonical TDX quote data")
            price = _scaled(row.price_scaled, row.price_scale)
            pre_close = _scaled(row.pre_close_scaled, row.detail_price_scale)
            if price <= 0 or pre_close <= 0:
                raise MetricInputLineageError("VALID primary quote contains a non-positive price")
            values[instrument_uid] = (price, pre_close)
            epochs.add(str(row.epoch_uid))
        if len(epochs) > 1:
            raise MetricInputLineageError("Primary Universe quotes span source epochs")
        return values, next(iter(epochs), None)

    def _context_quotes(self, quote_uids: Sequence[str]) -> dict[str, Decimal | None]:
        if not quote_uids:
            return {}
        with self._runtime.read_connection() as connection:
            rows = _rows_for_identifiers(
                connection,
                "SELECT q.quote_uid,l.instrument_uid,q.price_status,q.price_scaled,"
                "q.price_scale,d.quote_status FROM market_quote q "
                "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
                "LEFT JOIN tdx_quote_detail d ON d.quote_uid=q.quote_uid "
                "WHERE q.quote_uid IN ({placeholders}) ORDER BY q.quote_uid",
                quote_uids,
            )
        if len(rows) != len(quote_uids):
            raise MetricInputLineageError("manifest references an unknown canonical context quote")
        values: dict[str, Decimal | None] = {}
        for row in rows:
            instrument_uid = str(row.instrument_uid)
            if instrument_uid in values:
                raise MetricInputLineageError(
                    "context quote lineage must not duplicate an instrument"
                )
            if (
                str(row.price_status) != "VALUE"
                or row.price_scaled is None
                or str(row.quote_status) != "VALUE"
            ):
                values[instrument_uid] = None
                continue
            price = _scaled(row.price_scaled, row.price_scale)
            values[instrument_uid] = price if price > 0 else None
        return values

    def _context_metrics(
        self,
        snapshot: _Snapshot,
        sector_uid: str,
        quotes: Mapping[str, Decimal | None],
        grouped_bars: Mapping[tuple[str, str], Sequence[_Bar]],
        clock: TradingClock,
        exchange: str,
        current_session: tuple[str, datetime, datetime] | None,
    ) -> tuple[ContextMetric | None, ContextMetric | None, bool, dict[str, object]]:
        instant = format_rfc3339(snapshot.as_of_time)
        with self._runtime.read_connection() as connection:
            mapping = connection.exec_driver_sql(
                "SELECT m.mapping_version_uid,m.etf_instrument_uid,m.style_instrument_uid,"
                "m.owner_approval_ref,m.source_artifact_sha256 "
                "FROM reference_version_entry e "
                "JOIN sector_context_mapping_version m ON m.mapping_version_uid=e.version_uid "
                "WHERE e.bundle_uid=? AND e.entity_kind='MAPPING' AND e.entity_uid=? "
                "AND m.sector_uid=? AND m.valid_from<=? "
                "AND (m.valid_until IS NULL OR m.valid_until>?)",
                (snapshot.bundle_uid, sector_uid, sector_uid, instant, instant),
            ).one_or_none()
            pinned = {
                str(row.entity_uid)
                for row in connection.exec_driver_sql(
                    "SELECT entity_uid FROM reference_version_entry "
                    "WHERE bundle_uid=? AND entity_kind='INSTRUMENT'",
                    (snapshot.bundle_uid,),
                ).all()
            }
        if mapping is None:
            return (
                None,
                None,
                True,
                {
                    "status": "NOT_APPLICABLE",
                    "reason": "NO_OWNER_APPROVED_MAPPING",
                },
            )

        def context_metric(instrument_uid: object) -> tuple[ContextMetric | None, bool]:
            if instrument_uid is None:
                return None, True
            uid = str(instrument_uid)
            bars = tuple(
                sorted(
                    grouped_bars.get((uid, "1m"), ()),
                    key=lambda item: item.source_time,
                )
            )
            tail = _complete_current_tail(
                bars,
                snapshot.as_of_time,
                clock,
                exchange,
                current_session,
            )
            price = quotes.get(uid)
            if (
                uid not in pinned
                or price is None
                or tail is None
                or len(tail) < 5
                or tail[-5].close <= 0
            ):
                return ContextMetric(None), False
            return ContextMetric(price / tail[-5].close - Decimal("1")), True

        etf_context, etf_fit = context_metric(mapping.etf_instrument_uid)
        style_context, style_fit = context_metric(mapping.style_instrument_uid)
        return (
            etf_context,
            style_context,
            etf_fit and style_fit,
            {
                "status": "VALUE" if etf_fit and style_fit else "MISSING_CONTEXT_DATA",
                "version_uid": str(mapping.mapping_version_uid),
                "owner_approval_ref": str(mapping.owner_approval_ref),
                "source_artifact_sha256": str(mapping.source_artifact_sha256),
                "etf_instrument_uid": None
                if mapping.etf_instrument_uid is None
                else str(mapping.etf_instrument_uid),
                "style_instrument_uid": None
                if mapping.style_instrument_uid is None
                else str(mapping.style_instrument_uid),
            },
        )

    def _bars(self, bar_uids: Sequence[str]) -> tuple[tuple[_Bar, ...], str | None]:
        with self._runtime.read_connection() as connection:
            rows = _rows_for_identifiers(
                connection,
                "SELECT bar_uid,epoch_uid,instrument_uid,interval_kind,source_time,"
                "close_scaled,price_scale,amount_scaled,amount_scale FROM tdx_bar "
                "WHERE bar_uid IN ({placeholders}) "
                "ORDER BY instrument_uid,interval_kind,source_time,bar_uid",
                bar_uids,
            )
        if len(rows) != len(bar_uids):
            raise MetricInputLineageError("manifest references an unknown canonical bar")
        epochs = {str(row.epoch_uid) for row in rows}
        if len(epochs) != 1:
            raise MetricInputLineageError("manifest bars span source epochs")
        return (
            tuple(
                _Bar(
                    str(row.instrument_uid),
                    str(row.interval_kind),
                    parse_rfc3339(str(row.source_time)),
                    _scaled(row.close_scaled, row.price_scale),
                    _scaled(row.amount_scaled, row.amount_scale),
                    str(row.epoch_uid),
                )
                for row in rows
            ),
            next(iter(epochs)),
        )


def _continuity_observation(
    observation_uid: str,
    observed_at: datetime,
    trading_date: str,
    market_minute_index: int | None,
    fitness_status: object,
    base_facts: object,
) -> ContinuityObservation | None:
    if (
        market_minute_index is None
        or not isinstance(base_facts, Mapping)
        or not isinstance(fitness_status, str)
        or fitness_status not in {"FIT", "FIT_WITH_LIMITATIONS", "UNFIT"}
    ):
        return None
    sector_return = _evidence_decimal(base_facts.get("sector_return_5m"))
    market_return = _evidence_decimal(base_facts.get("market_return_5m"))
    try:
        return ContinuityObservation(
            observation_uid=observation_uid,
            observed_at=observed_at,
            trading_date=trading_date,
            market_minute_index=market_minute_index,
            fitness_status=cast(Literal["FIT", "FIT_WITH_LIMITATIONS", "UNFIT"], fitness_status),
            relative_strength_5m=(
                None
                if sector_return is None or market_return is None
                else sector_return - market_return
            ),
            breadth_5m_up=_evidence_decimal(base_facts.get("breadth_5m")),
            turnover_ratio_5m=_evidence_decimal(base_facts.get("turnover_ratio_5m")),
        )
    except ValueError:
        return None


def _evidence_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _identifier_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise MetricInputLineageError(f"manifest {name} must be stable identifiers")
    if len(set(value)) != len(value):
        raise MetricInputLineageError(f"manifest {name} must not duplicate")
    return tuple(value)


def _primary_statuses(
    manifest: Mapping[str, Any],
) -> dict[str, tuple[str, str | None]]:
    value = manifest.get("primary_quote_statuses")
    if not isinstance(value, list) or not value:
        raise MetricInputLineageError("manifest requires Primary Universe quote status inventory")
    statuses: dict[str, tuple[str, str | None]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise MetricInputLineageError("invalid primary quote status record")
        instrument_uid = item.get("instrument_uid")
        quote_uid = item.get("quote_uid")
        status = item.get("status")
        if (
            not isinstance(instrument_uid, str)
            or not instrument_uid
            or not isinstance(status, str)
            or status not in _QUOTE_STATUSES
            or quote_uid is not None
            and (not isinstance(quote_uid, str) or not quote_uid)
            or (status == "VALID") != isinstance(quote_uid, str)
            or instrument_uid in statuses
        ):
            raise MetricInputLineageError("invalid primary quote status lineage")
        statuses[instrument_uid] = (status, quote_uid)
    return dict(sorted(statuses.items()))


def _historical_dates(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    windows = manifest.get("historical_windows")
    if not isinstance(windows, Mapping):
        raise MetricInputLineageError("manifest historical windows are required")
    value = windows.get("turnover_same_clock")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MetricInputLineageError("manifest turnover same-clock windows are required")
    if len(set(value)) != len(value):
        raise MetricInputLineageError("manifest turnover same-clock windows must not duplicate")
    return tuple(sorted(value))


def _complete_current_tail(
    bars: Sequence[_Bar],
    as_of: datetime,
    clock: TradingClock,
    exchange: str,
    active_session: tuple[str, datetime, datetime] | None = None,
) -> tuple[_Bar, ...] | None:
    complete = tuple(
        sorted(
            (
                item
                for item in bars
                if item.source_time <= as_of
                and item.source_time.astimezone(_CHINA).date() == as_of.astimezone(_CHINA).date()
            ),
            key=lambda item: item.source_time,
        )
    )
    if not complete:
        return None
    session = active_session or clock.continuous_session_at(exchange, as_of)
    if session is None:
        session = clock.continuous_session_at(
            exchange, complete[-1].source_time - timedelta(microseconds=1)
        )
    if session is None:
        return None
    _, opens_at, closes_at = session
    current_session = tuple(item for item in complete if opens_at < item.source_time <= closes_at)
    if not current_session:
        return None
    tail = [current_session[-1]]
    for item in reversed(current_session[:-1]):
        if tail[0].source_time - item.source_time != timedelta(minutes=1):
            break
        tail.insert(0, item)
        if len(tail) == 15:
            break
    return tuple(tail)


def _same_clock_amount(
    bars: Sequence[_Bar], trading_date: str, current_end: datetime
) -> Decimal | None:
    try:
        target = datetime.fromisoformat(
            f"{trading_date}T{current_end.astimezone(_CHINA):%H:%M}:00+08:00"
        ).astimezone(UTC)
    except ValueError as error:
        raise MetricInputLineageError("invalid historical trading date") from error
    by_time = {item.source_time: item for item in bars}
    expected = tuple(target - timedelta(minutes=offset) for offset in range(4, -1, -1))
    if any(timestamp not in by_time for timestamp in expected):
        return None
    return sum((by_time[timestamp].amount for timestamp in expected), Decimal("0"))


def _scaled(value: object, scale: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetricInputLineageError("canonical numeric value is invalid")
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise MetricInputLineageError("canonical numeric scale is invalid")
    return Decimal(value).scaleb(-scale)
