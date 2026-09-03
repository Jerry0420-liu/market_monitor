"""Explicit local Native TDX acquisition and shadow validation command."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic_ns
from typing import Any, Literal
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for source in reversed(
    (
        ROOT / "packages" / "analysis" / "src",
        ROOT / "packages" / "data" / "src",
        ROOT / "packages" / "persistence" / "src",
    )
):
    sys.path.insert(0, str(source))

from market_monitor_analysis.calibration import CalibrationRunner  # noqa: E402
from market_monitor_analysis.metric_runner import MetricRunner, MetricRunResult  # noqa: E402
from market_monitor_analysis.snapshots import PrimaryQuoteStatus, SnapshotBuilder  # noqa: E402
from market_monitor_analysis.thresholds import ThresholdRegistry  # noqa: E402
from market_monitor_data.clock import TradingClock  # noqa: E402
from market_monitor_data.health import (  # noqa: E402
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
    WatermarkRepository,
)
from market_monitor_data.ingestion import IngestionService  # noqa: E402
from market_monitor_data.models import ProviderInstrumentRegistration  # noqa: E402
from market_monitor_data.reference import ReferenceRepository  # noqa: E402
from market_monitor_data.tdx.client import TdxLiveClient  # noqa: E402
from market_monitor_data.tdx.historical import TdxHistoricalLoader  # noqa: E402
from market_monitor_data.tdx.protocol import TdxProtocol  # noqa: E402
from market_monitor_data.tdx.provider import (  # noqa: E402
    NativeTdxProvider,
    TdxGateway,
    TdxMinuteIncrementResult,
)
from market_monitor_data.tdx.storage import TdxStorage  # noqa: E402
from market_monitor_data.tdx.transport import (  # noqa: E402
    TdxPoolStatistics,
    TdxServer,
    TdxServerPool,
)
from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.values import format_rfc3339, new_uid, parse_rfc3339  # noqa: E402
from market_monitor_persistence.writer import WriterQueue  # noqa: E402

DEFAULT_SERVERS = (
    TdxServer("180.153.18.170"),
    TdxServer("218.6.170.47"),
    TdxServer("123.125.108.14"),
)
_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_REALTIME_OBSERVATION_MAX_AGE = timedelta(seconds=60)
_REALTIME_OBSERVATION_MAX_SKEW_MS = 60_000
REPRESENTATIVE_PROBES = frozenset({"SH_STOCK", "SZ_STOCK", "INDEX", "ETF", "MINUTE_BAR"})
_SIDE_EFFECT_TABLES = (
    "analysis_subject",
    "analysis_commit",
    "event_version",
    "notification_intent",
    "delivery_attempt",
)
_OFFICIAL_BUSINESS_AUDIT_QUERIES = {
    "analysis_commit": "SELECT count(*) FROM analysis_commit",
    "capability_watermark": "SELECT count(*) FROM capability_watermark",
    "current_event_projection": "SELECT count(*) FROM current_event_projection",
    "current_state_projection": "SELECT count(*) FROM current_state_projection",
    "delivery_attempt": "SELECT count(*) FROM delivery_attempt",
    "event_evidence": "SELECT count(*) FROM event_evidence",
    "event_version": "SELECT count(*) FROM event_version",
    "guardian_evaluation": (
        "SELECT count(*) FROM guardian_evaluation g JOIN state_evaluation s "
        "ON s.evaluation_uid=g.state_evaluation_uid "
        "WHERE s.evaluation_disposition='OFFICIAL'"
    ),
    "market_event": "SELECT count(*) FROM market_event",
    "notification_delivery_state": "SELECT count(*) FROM notification_delivery_state",
    "notification_intent": "SELECT count(*) FROM notification_intent",
    "scout_evaluation": (
        "SELECT count(*) FROM scout_evaluation s JOIN state_evaluation e "
        "ON e.evaluation_uid=s.state_evaluation_uid "
        "WHERE e.evaluation_disposition='OFFICIAL'"
    ),
    "state_evaluation": (
        "SELECT count(*) FROM state_evaluation WHERE evaluation_disposition='OFFICIAL'"
    ),
    "threshold_activation": "SELECT count(*) FROM threshold_activation",
}
_REFERENCE_ONBOARDING_AUDIT_QUERIES = {
    "analysis_subject": "SELECT count(*) FROM analysis_subject",
    "provider_sector_mapping": ("SELECT count(*) FROM provider_mapping WHERE entity_kind='SECTOR'"),
    "sector": "SELECT count(*) FROM sector",
    "sector_membership": "SELECT count(*) FROM sector_membership",
    "sector_membership_version": "SELECT count(*) FROM sector_membership_version",
    "sector_version": "SELECT count(*) FROM sector_version",
    "tdx_sector_membership_source": "SELECT count(*) FROM tdx_sector_membership_source",
}


def _require_continuous_shadow_round(clock: TradingClock, observed_at: datetime) -> None:
    phases = {exchange: clock.phase_at(exchange, observed_at) for exchange in ("SSE", "SZSE")}
    if "CALENDAR_COVERAGE_MISSING" in phases.values():
        raise ValueError(f"CALENDAR_COVERAGE_MISSING: Shadow phase unavailable; phases={phases}")
    if phases["SSE"] not in {"CONTINUOUS_AM", "CONTINUOUS_PM"} or phases["SSE"] != phases["SZSE"]:
        raise ValueError("CR-004 metric Shadow rounds require an aligned continuous market session")


def _require_calendar_coverage_ready(
    clock: TradingClock, observed_at: datetime
) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    failures: list[str] = []
    for exchange in ("SSE", "SZSE"):
        readiness = clock.calendar_coverage_readiness(exchange, observed_at)
        report[exchange] = {
            "state": readiness.state,
            "observed_date": readiness.observed_date,
            "coverage_start": readiness.coverage_start,
            "coverage_end": readiness.coverage_end,
            "future_trading_days": readiness.future_trading_days,
            "minimum_future_trading_days": readiness.minimum_future_trading_days,
            "missing_dates": list(readiness.missing_dates),
        }
        if not readiness.ready:
            failures.append(f"{exchange}={readiness.state}")
    if failures:
        state = (
            "CALENDAR_COVERAGE_MISSING"
            if any(value.endswith("CALENDAR_COVERAGE_MISSING") for value in failures)
            else "CALENDAR_COVERAGE_INSUFFICIENT"
        )
        raise ValueError(f"{state}: approved calendar readiness failed ({', '.join(failures)})")
    return report


def _require_current_primary_minute_cohort(
    clock: TradingClock,
    targets: tuple[tuple[int, datetime], ...],
    observed_at: datetime,
) -> None:
    if _primary_minute_targets(clock, observed_at) != targets:
        raise ValueError(
            "CR-004 metric Shadow minute cohort became stale before snapshot preparation"
        )


def _primary_minute_targets(
    clock: TradingClock, observed_at: datetime
) -> tuple[tuple[int, datetime], ...] | None:
    targets = tuple(
        (market, clock.latest_completed_continuous_minute(exchange, observed_at))
        for market, exchange in ((0, "SZSE"), (1, "SSE"))
    )
    if any(target is None for _, target in targets):
        return None
    return tuple((market, target) for market, target in targets if target is not None)


def _bootstrap_current_live_tail(
    provider: NativeTdxProvider,
    clock: TradingClock,
    epoch_uid: str,
    observed_at: datetime,
) -> TdxMinuteIncrementResult:
    """Fill only the current legal 1m tail after historical readiness is already FIT."""
    targets = _primary_minute_targets(clock, observed_at)
    if targets is None:
        raise ValueError("CR-004 live minute bootstrap requires a completed legal SSE/SZSE minute")
    tails = {
        market: clock.completed_legal_minute_tail("SZSE" if market == 0 else "SSE", target, 15)
        for market, target in targets
    }
    if any(not tail or tail[-1] != dict(targets)[market] for market, tail in tails.items()):
        raise ValueError("CR-004 live minute bootstrap produced an invalid legal-minute tail")
    result = provider.bootstrap_primary_minute_tail(epoch_uid, dict(targets), tails, observed_at)
    if result.health_capability == "UNHEALTHY":
        raise ValueError("CR-004 live minute tail is unfit")
    return result


def _minute_cohort_record(result: TdxMinuteIncrementResult) -> dict[str, Any]:
    return {
        "targets": [
            {"market": market, "source_time": format_rfc3339(source_time)}
            for market, source_time in result.targets
        ],
        "expected_primary_count": result.expected_count,
        "known_suspended_count": result.known_suspended_count,
        "valid_latest_complete_count": result.valid_latest_complete_count,
        "missing_or_invalid_count": result.missing_or_invalid_count,
        "coverage_ppm": result.coverage_ppm,
        "source_time_integrity": "EXACT_TARGET",
        "request_count": result.request_count,
        "worker_count": result.worker_count,
        "duration_ms": result.duration_ms,
        "failure_counts": dict(result.failure_counts),
    }


def _import_listing_reference_facts(
    path: Path,
    references: ReferenceRepository,
    artifacts: ArtifactStore,
    observed_at: datetime,
) -> dict[str, object]:
    """Import an explicit local listing fact document and retain its exact bytes."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError("listing Reference fact file does not exist")
    payload = resolved.read_bytes()
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("listing Reference fact file is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("listing Reference fact schema_version must be 1")
    provenance = document.get("provenance")
    facts = document.get("facts")
    if not isinstance(provenance, dict):
        raise ValueError("listing Reference fact provenance is required")
    source_ref = _required_text(provenance, "source_ref", "listing Reference provenance")
    if not isinstance(facts, list) or not facts:
        raise ValueError("listing Reference facts must be a non-empty list")
    registrations: list[ProviderInstrumentRegistration] = []
    for item in facts:
        if not isinstance(item, dict):
            raise ValueError("listing Reference fact entries must be objects")
        registrations.append(
            ProviderInstrumentRegistration(
                _required_text(item, "external_code", "listing Reference fact"),
                _required_text(item, "instrument_kind", "listing Reference fact"),
                _required_text(item, "exchange", "listing Reference fact"),
                _required_text(item, "trading_code", "listing Reference fact"),
                _required_text(item, "name", "listing Reference fact"),
                _required_text(item, "listing_status", "listing Reference fact"),
                _required_text(item, "trading_status", "listing Reference fact"),
                parse_rfc3339(
                    _required_text(item, "listing_effective_at", "listing Reference fact")
                ),
            )
        )
    artifact = artifacts.put_bytes(payload, "application/vnd.market-monitor.reference+json")
    artifacts.register(artifact)
    imported = references.import_listing_reference_facts(
        NativeTdxProvider.PROVIDER_KEY,
        tuple(registrations),
        observed_at,
        source_artifact_sha256=artifact.sha256,
        source_ref=source_ref,
    )
    return {
        "schema_version": 1,
        "artifact_sha256": artifact.sha256,
        "source_ref": source_ref,
        "fact_count": len(registrations),
        "new_instrument_count": imported,
    }


def _required_text(document: Mapping[str, object], field: str, scope: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{scope} requires {field}")
    return value


def run_shadow(
    data_directory: Path,
    gateway: TdxGateway,
    *,
    sweeps: int,
    metric_shadow: bool = False,
    guardian_threshold_version: str | None = None,
    scout_threshold_version: str | None = None,
    metric_iterations: int = 1,
    trading_calendar_file: Path | None = None,
    listing_reference_file: Path | None = None,
    now: Callable[[], datetime] | None = None,
    node_health: Callable[[], Sequence[object]] | None = None,
    transport_statistics: Callable[[], object] | None = None,
    minute_gateways: Sequence[TdxGateway] | None = None,
) -> dict[str, Any]:
    """Run acquisition only; frozen analysis and notification stages stay untouched."""
    if sweeps < 1 or metric_iterations < 1:
        raise ValueError("TDX sweeps must be positive")
    if metric_shadow and (not guardian_threshold_version or not scout_threshold_version):
        raise ValueError("--metric-shadow requires explicit Guardian and Scout threshold versions")
    if metric_shadow and sweeps > 1 and metric_iterations != 1:
        raise ValueError("repeatability iterations cannot substitute for acceptance rounds")
    clock = now or (lambda: datetime.now(UTC))
    paths = DatabasePaths.from_data_directory(data_directory)
    runtime = DatabaseRuntime.open(paths)
    writer: WriterQueue | None = None
    try:
        MigrationManager().upgrade(runtime)
        MigrationManager().verify(runtime)
        writer = WriterQueue(runtime)
        writer.start()
        artifacts = ArtifactStore(runtime, writer)
        calendar = (
            _import_trading_calendar(trading_calendar_file, runtime, writer)
            if trading_calendar_file is not None
            else None
        )
        trading_clock = TradingClock(runtime, writer)
        observed_at = clock()
        calendar_readiness = None
        if metric_shadow:
            calendar_readiness = _require_calendar_coverage_ready(trading_clock, observed_at)
            _require_continuous_shadow_round(trading_clock, observed_at)
        references = ReferenceRepository(runtime, writer)
        listing_reference = (
            _import_listing_reference_facts(
                listing_reference_file, references, artifacts, observed_at
            )
            if listing_reference_file is not None
            else None
        )
        provider = NativeTdxProvider(
            runtime,
            references,
            IngestionService(runtime, writer, artifacts),
            artifacts,
            CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 40_000, 60)),
            TdxStorage(runtime, writer),
            gateway,
            clock=trading_clock,
            minute_gateways=minute_gateways,
        )
        before = _side_effect_counts(runtime)
        audit_before = _audit_surface_counts(runtime) if metric_shadow else None
        probes = _representative_probes(gateway, observed_at)
        universe = provider.synchronize_reference(observed_at)
        epoch_uid = _active_tdx_epoch(runtime) or SourceEpochService(runtime, writer).start_epoch(
            NativeTdxProvider.PROVIDER_KEY, "cr002-native-tdx-v1", observed_at
        )
        warmup = None
        if metric_shadow:
            warmup = TdxHistoricalLoader(
                TdxStorage(runtime, writer), gateway, trading_clock
            ).assess(epoch_uid, universe, clock())
            if warmup.state != "FIT":
                raise ValueError(
                    f"CR-004 metric Shadow requires FIT historical warm-up; received {warmup.state}"
                )
            _bootstrap_current_live_tail(provider, trading_clock, epoch_uid, clock())
        supporting_at = clock()
        supporting = provider.refresh_supporting_data(
            epoch_uid, supporting_at, onboard_official_sectors=metric_shadow
        )
        watermarks = {}
        if not metric_shadow:
            watermarks = _advance_bar_watermarks(runtime, writer, epoch_uid, supporting_at)
        results = []
        shadow_snapshot_uids: list[str] = []
        metric_results: list[MetricRunResult] = []
        shadow_rounds: list[dict[str, Any]] = []
        latest_minute_cohort: TdxMinuteIncrementResult | None = None
        primary_universe_version = hashlib.sha256(
            "\n".join(
                f"{item.market}:{item.code}:{item.kind}"
                for item in sorted(
                    universe.primary,
                    key=lambda item: (item.market, item.code, item.kind),
                )
            ).encode("ascii")
        ).hexdigest()
        for _ in range(sweeps):
            round_uid = new_uid()
            round_started_ns = monotonic_ns()
            round_observed_at = clock()
            if not metric_shadow:
                result = provider.poll_once(epoch_uid, round_observed_at)
                results.append(result)
                continue

            _require_continuous_shadow_round(trading_clock, round_observed_at)
            minute_targets = _primary_minute_targets(trading_clock, round_observed_at)
            if minute_targets is None:
                raise ValueError("CR-004 metric Shadow requires a completed legal SSE/SZSE minute")
            round_epoch_uid = epoch_uid
            round_primary_universe_version = primary_universe_version
            acquisition_started_ns = monotonic_ns()
            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="tdx-shadow-acquisition"
            ) as executor:
                quote_future = executor.submit(
                    provider.poll_once, round_epoch_uid, round_observed_at
                )
                minute_future = executor.submit(
                    provider.refresh_primary_minute_bars,
                    round_epoch_uid,
                    dict(minute_targets),
                    round_observed_at,
                )
                result = quote_future.result()
                latest_minute_cohort = minute_future.result()
            acquisition_elapsed_ms = (monotonic_ns() - acquisition_started_ns) // 1_000_000
            results.append(result)
            if latest_minute_cohort is None:
                raise ValueError("CR-004 metric Shadow has no Primary Universe minute cohort")
            if tuple(latest_minute_cohort.targets) != minute_targets:
                raise ValueError("CR-004 metric Shadow minute cohort target mismatch")
            if latest_minute_cohort.health_capability not in {"HEALTHY", "DEGRADED"}:
                raise ValueError("CR-004 metric Shadow minute cohort incomplete")

            prepared_at = clock()
            _require_continuous_shadow_round(trading_clock, prepared_at)
            _require_current_primary_minute_cohort(
                trading_clock,
                minute_targets,
                prepared_at,
            )
            preparation_started_ns = monotonic_ns()
            round_snapshots = _prepare_metric_shadow_snapshots(
                runtime,
                writer,
                artifacts,
                round_epoch_uid,
                prepared_at,
                round_uid,
                minute_cohort=latest_minute_cohort,
            )
            preparation_elapsed_ms = (monotonic_ns() - preparation_started_ns) // 1_000_000
            metric_started_ns = monotonic_ns()
            round_results = _metric_shadow_results(
                runtime,
                writer,
                artifacts,
                guardian_threshold_version,
                scout_threshold_version,
                metric_iterations,
                round_snapshots,
            )
            metric_elapsed_ms = (monotonic_ns() - metric_started_ns) // 1_000_000
            shadow_snapshot_uids.extend(round_snapshots)
            metric_results.extend(round_results)
            shadow_rounds.append(
                {
                    "round_uid": round_uid,
                    "observed_at": format_rfc3339(round_observed_at),
                    "prepared_at": format_rfc3339(prepared_at),
                    "market_source_epoch": round_epoch_uid,
                    "primary_universe_version": round_primary_universe_version,
                    "minute_cohort": _minute_cohort_record(latest_minute_cohort),
                    "snapshot_uids": list(round_snapshots),
                    "metric_run_count": len(round_results),
                    "acquisition_elapsed_ms": acquisition_elapsed_ms,
                    "quote_acquisition_elapsed_ms": int(result.duration_ms),
                    "minute_cohort_elapsed_ms": int(latest_minute_cohort.duration_ms),
                    "preparation_elapsed_ms": preparation_elapsed_ms,
                    "metric_elapsed_ms": metric_elapsed_ms,
                    "metric_evaluation_elapsed_ms": sum(
                        item.metric_evaluation_ms for item in round_results
                    ),
                    "threshold_evaluation_elapsed_ms": sum(
                        item.threshold_evaluation_ms for item in round_results
                    ),
                    "metric_evidence_persistence_elapsed_ms": sum(
                        item.evidence_persistence_ms for item in round_results
                    ),
                    "elapsed_ms": (monotonic_ns() - round_started_ns) // 1_000_000,
                }
            )
        metric_runs = [_metric_run_record(result) for result in metric_results]
        after = _side_effect_counts(runtime)
        durations = [result.duration_ms for result in results]
        capability_states = dict(supporting.health_capabilities)
        if results:
            capability_states.update(results[-1].health_capabilities)
        if latest_minute_cohort is not None:
            capability_states["MINUTE_BARS"] = latest_minute_cohort.health_capability
        report: dict[str, Any] = {
            "ok": True,
            "mode": "shadow" if sweeps > 1 else "once",
            "epoch_uid": epoch_uid,
            "universe": {
                "primary": len(universe.primary),
                "context_indexes": len(universe.context_indexes),
                "context_etfs": len(universe.context_etfs),
            },
            "probes": probes,
            "listing_reference": listing_reference,
            "sweeps": {
                "count": len(results),
                "requested": [result.requested for result in results],
                "returned": [result.returned for result in results],
                "missing": [result.requested - result.returned for result in results],
                "quarantined": [result.quarantined for result in results],
                "durations_ms": durations,
                "p50_ms": _percentile(durations, 50),
                "p95_ms": _percentile(durations, 95),
                "max_ms": max(durations, default=0),
            },
            "capabilities": capability_states,
            "blocks": {"new_versions": supporting.block_versions},
            "watermarks": watermarks,
            "freshness": _freshness(runtime, epoch_uid),
            "transport": _transport_report(node_health, transport_statistics),
            "evidence": _evidence_counts(runtime, epoch_uid),
            "side_effect_delta": {name: after[name] - before[name] for name in before},
        }
        if metric_shadow:
            assert warmup is not None
            report["warmup"] = {
                "state": warmup.state,
                "minute_complete": warmup.minute_complete,
                "daily_complete": warmup.daily_complete,
                "coverage_ppm": warmup.coverage_ppm,
                "catchup_uid": getattr(warmup, "catchup_uid", None),
                "coverage_checked": getattr(warmup, "coverage_checked", 0),
                "symbols_skipped": getattr(warmup, "symbols_skipped", 0),
                "fetch_requests": getattr(warmup, "fetch_requests", 0),
                "fetched_bars": getattr(warmup, "fetched_bars", 0),
                "inserted_bars": getattr(warmup, "inserted_bars", 0),
                "duplicate_bars": getattr(warmup, "duplicate_bars", 0),
                "missing_symbols": getattr(warmup, "missing_symbols", 0),
                "catchup_duration_ms": getattr(warmup, "catchup_duration_ms", 0),
            }
            report["shadow_snapshots"] = list(shadow_snapshot_uids)
            report["shadow_rounds"] = shadow_rounds
            report["metric_repeatability"] = {
                "mode": "REPEATABILITY" if metric_iterations > 1 else "FRESH_ROUND",
                "iterations": metric_iterations,
                "acceptance_rounds": sweeps,
                "is_repeatability_test": metric_iterations > 1,
            }
            report["metrics"] = metric_timing_summary(
                [int(item["elapsed_ms"]) for item in metric_runs]
            )
            report["metric_runs"] = metric_runs
            calibration_started_ns = monotonic_ns()
            report["calibration"] = _shadow_calibration_report(
                runtime,
                writer,
                artifacts,
                metric_results,
                guardian_threshold_version,
                scout_threshold_version,
                clock(),
            )
            calibration_elapsed_ms = (monotonic_ns() - calibration_started_ns) // 1_000_000
            assert audit_before is not None
            audit = _side_effect_audit(audit_before, _audit_surface_counts(runtime))
            if not audit["passed"]:
                raise RuntimeError("metric Shadow mutated OFFICIAL business state")
            report["side_effect_audit"] = audit
            report["timing"] = _shadow_timing_summary(
                shadow_rounds, metric_results, calibration_elapsed_ms
            )
        if calendar is not None:
            report["calendar"] = calendar
        if calendar_readiness is not None:
            report["calendar_readiness"] = calendar_readiness
        return report
    finally:
        if writer is not None:
            writer.close()
        runtime.close()


def _import_trading_calendar(
    path: Path, runtime: DatabaseRuntime, writer: WriterQueue
) -> dict[str, int | str]:
    if not path.is_file():
        raise ValueError("trading calendar file is required and must be a regular file")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("trading calendar file must be UTF-8 JSON") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("trading calendar schema_version must be 1")
    generated_at, content_hash = _validate_calendar_provenance(document)
    days = document.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError("trading calendar requires a non-empty days list")

    entries: list[tuple[str, str, str, list[tuple[str, str, str]]]] = []
    seen: set[tuple[str, str]] = set()
    for item in days:
        if not isinstance(item, dict):
            raise ValueError("trading calendar day must be an object")
        exchange = _calendar_text(item.get("exchange"), "exchange")
        trading_date = _calendar_text(item.get("trading_date"), "trading_date")
        timezone = _calendar_text(item.get("timezone"), "timezone")
        if exchange not in {"SSE", "SZSE"} or timezone != "Asia/Shanghai":
            raise ValueError("calendar supports only SSE/SZSE Asia/Shanghai days")
        date.fromisoformat(trading_date)
        key = (exchange, trading_date)
        if key in seen:
            raise ValueError("trading calendar contains a duplicate exchange/date")
        seen.add(key)
        raw_sessions = item.get("sessions")
        if not isinstance(raw_sessions, list):
            raise ValueError("trading calendar sessions must be a list")
        sessions: list[tuple[str, str, str]] = []
        for session in raw_sessions:
            if not isinstance(session, dict):
                raise ValueError("trading calendar session must be an object")
            sessions.append(
                (
                    _calendar_text(session.get("phase"), "session phase"),
                    _calendar_text(session.get("opens_at"), "session opens_at"),
                    _calendar_text(session.get("closes_at"), "session closes_at"),
                )
            )
        entries.append((exchange, trading_date, timezone, sessions))

    dates_by_exchange = {
        exchange: {
            date.fromisoformat(trading_date)
            for item_exchange, trading_date in seen
            if item_exchange == exchange
        }
        for exchange in ("SSE", "SZSE")
    }
    if not dates_by_exchange["SSE"] or dates_by_exchange["SSE"] != dates_by_exchange["SZSE"]:
        raise ValueError("trading calendar must cover identical non-empty SSE/SZSE date ranges")
    calendar_start = min(dates_by_exchange["SSE"])
    calendar_end = max(dates_by_exchange["SSE"])
    expected_dates = {
        calendar_start + timedelta(days=offset)
        for offset in range((calendar_end - calendar_start).days + 1)
    }
    if dates_by_exchange["SSE"] != expected_dates:
        raise ValueError("trading calendar must contain an explicit SSE/SZSE row for every date")

    clock = TradingClock(runtime, writer)
    inserted = sum(
        clock.import_day(exchange, trading_date, timezone, sessions)
        for exchange, trading_date, timezone, sessions in sorted(entries)
    )
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_hash": content_hash,
        "generated_at": generated_at,
        "days": len(entries),
        "inserted_days": inserted,
        "coverage_start": calendar_start.isoformat(),
        "coverage_end": calendar_end.isoformat(),
    }


def _calendar_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"trading calendar {name} is required")
    return value


def _validate_calendar_provenance(document: Mapping[str, Any]) -> tuple[str, str]:
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("trading calendar provenance is required")
    _calendar_text(provenance.get("authority"), "provenance authority")
    sources = provenance.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("trading calendar provenance sources are required")
    exchanges: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("trading calendar provenance source must be an object")
        exchange = _calendar_text(source.get("exchange"), "provenance source exchange")
        domain = {"SSE": "sse.com.cn", "SZSE": "szse.cn"}.get(exchange)
        if domain is None:
            raise ValueError("trading calendar provenance supports only SSE/SZSE sources")
        _calendar_text(source.get("title"), "provenance source title")
        parsed = urlparse(_calendar_text(source.get("url"), "provenance source url"))
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or host is None
            or not (host == domain or host.endswith(f".{domain}"))
        ):
            raise ValueError(
                "trading calendar provenance source URL must be an official SSE/SZSE HTTPS URL"
            )
        exchanges.add(exchange)
    if exchanges != {"SSE", "SZSE"}:
        raise ValueError("trading calendar provenance must include SSE and SZSE sources")

    generated_at = _calendar_text(document.get("generated_at"), "generated_at")
    try:
        if format_rfc3339(parse_rfc3339(generated_at)) != generated_at:
            raise ValueError("not canonical")
    except ValueError as error:
        raise ValueError("trading calendar generated_at must be canonical RFC3339") from error
    content_hash = _calendar_text(document.get("content_hash"), "content_hash")
    if content_hash != _calendar_content_hash(document):
        raise ValueError("trading calendar content_hash does not match canonical content")
    return generated_at, content_hash


def _calendar_content_hash(document: Mapping[str, Any]) -> str:
    try:
        content = json.dumps(
            {key: value for key, value in document.items() if key != "content_hash"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("trading calendar content must be canonical JSON") from error
    return hashlib.sha256(content).hexdigest()


def _active_tdx_epoch(runtime: DatabaseRuntime) -> str | None:
    with runtime.read_connection() as connection:
        value = connection.exec_driver_sql(
            "SELECT epoch_uid FROM market_source_epoch "
            "WHERE provider_key=? AND status='ACTIVE' ORDER BY started_at DESC LIMIT 1",
            (NativeTdxProvider.PROVIDER_KEY,),
        ).scalar_one_or_none()
    return None if value is None else str(value)


def _prepare_metric_shadow_snapshots(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    epoch_uid: str,
    as_of: datetime,
    shadow_round_uid: str | None = None,
    *,
    minute_cohort: TdxMinuteIncrementResult | None = None,
    disposition: Literal["SHADOW", "OFFICIAL"] = "SHADOW",
    subject_uid: str | None = None,
) -> tuple[str, ...]:
    """Seal fresh CR-004 snapshots from one Native TDX epoch only."""
    if disposition not in {"SHADOW", "OFFICIAL"}:
        raise ValueError("metric snapshot disposition is invalid")
    if as_of.tzinfo is None:
        raise ValueError("CR-004 Shadow preparation requires a timezone-aware as-of time")
    instant = format_rfc3339(as_of)
    clock = TradingClock(runtime, writer)
    trading_date = as_of.astimezone(_SHANGHAI).date().isoformat()
    same_clock_dates = {
        exchange: clock.previous_valid_trading_dates(exchange, trading_date, 5)
        for exchange in ("SSE", "SZSE")
    }
    daily_dates = {
        exchange: clock.previous_valid_trading_dates(exchange, trading_date, 20)
        for exchange in ("SSE", "SZSE")
    }
    if (
        any(len(values) != 5 for values in same_clock_dates.values())
        or any(len(values) != 20 for values in daily_dates.values())
        or same_clock_dates["SSE"] != same_clock_dates["SZSE"]
        or daily_dates["SSE"] != daily_dates["SZSE"]
    ):
        raise ValueError("CR-004 Shadow preparation requires aligned SSE/SZSE calendar windows")

    statuses, quote_uids, quote_times = _shadow_primary_quote_statuses(runtime, epoch_uid, as_of)
    if not quote_uids:
        raise ValueError("CR-004 Shadow preparation has no valid Primary Universe quotes")
    bar_uids, realtime_current_bar_uids, realtime_current_times = _shadow_metric_bars(
        runtime,
        epoch_uid,
        as_of,
        same_clock_dates,
        daily_dates,
        minute_targets=None if minute_cohort is None else dict(minute_cohort.targets),
    )
    if minute_cohort is not None and len(realtime_current_bar_uids) != (
        minute_cohort.valid_latest_complete_count
    ):
        raise ValueError(
            "CR-004 Shadow minute cohort no longer matches canonical exact-target bars"
        )
    source_times = [*quote_times, *realtime_current_times]
    if not realtime_current_bar_uids or not source_times:
        raise ValueError("CR-004 Shadow preparation has no fresh current-minute observations")
    if any(
        source_time < as_of - _REALTIME_OBSERVATION_MAX_AGE or source_time > as_of
        for source_time in quote_times
    ):
        raise ValueError("CR-004 Shadow preparation has stale realtime observations")
    realtime_skew_ms = int((max(source_times) - min(source_times)).total_seconds() * 1_000)
    if realtime_skew_ms > _REALTIME_OBSERVATION_MAX_SKEW_MS:
        raise ValueError("CR-004 Shadow preparation realtime observation skew exceeds limit")
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    manifest_uid = snapshots.create_manifest(
        quote_uids,
        as_of,
        bar_uids=bar_uids,
        primary_quote_statuses=statuses,
        historical_windows={"turnover_same_clock": same_clock_dates["SSE"]},
        realtime_quote_uids=quote_uids,
        realtime_current_bar_uids=realtime_current_bar_uids,
        realtime_minute_cohort=(
            None if minute_cohort is None else _minute_cohort_record(minute_cohort)
        ),
        shadow_round_uid=shadow_round_uid,
    )
    with runtime.read_connection() as connection:
        subjects = connection.exec_driver_sql(
            "WITH latest_membership AS ("
            "SELECT v.sector_uid,v.membership_version_uid,"
            "row_number() OVER (PARTITION BY v.sector_uid ORDER BY v.version DESC) AS rank "
            "FROM sector_membership_version v "
            "JOIN tdx_sector_membership_source src "
            "ON src.membership_version_uid=v.membership_version_uid "
            "WHERE v.trading_date=? AND src.membership_kind "
            "IN ('ConceptMembership','ThemeMembership')"
            ") "
            "SELECT a.subject_uid,m.sector_uid,m.membership_version_uid,"
            "(SELECT sv.sector_version_uid FROM sector_version sv "
            "WHERE sv.sector_uid=m.sector_uid AND sv.valid_from<=? "
            "AND (sv.valid_until IS NULL OR sv.valid_until>?) "
            "ORDER BY sv.valid_from DESC,sv.version DESC LIMIT 1) AS sector_version_uid "
            "FROM latest_membership m JOIN analysis_subject a ON a.sector_uid=m.sector_uid "
            "WHERE m.rank=1 AND a.subject_kind='SECTOR' ORDER BY a.subject_uid",
            (trading_date, instant, instant),
        ).all()
    if not subjects:
        raise ValueError("CR-004 Shadow preparation has no Concept/Theme sector subjects")
    if subject_uid is not None:
        subjects = tuple(item for item in subjects if str(item.subject_uid) == subject_uid)
        if not subjects:
            raise ValueError("requested official subject is not an eligible sector subject")

    snapshot_uids: list[str] = []
    for subject in subjects:
        if subject.sector_version_uid is None:
            raise ValueError("CR-004 Shadow preparation found sector without an active version")
        bundle_uid = snapshots.create_reference_bundle(
            [
                ("SECTOR", str(subject.sector_uid), str(subject.sector_version_uid)),
                ("MEMBERSHIP", str(subject.sector_uid), str(subject.membership_version_uid)),
                ("CALENDAR", "SSE", trading_date),
            ]
        )
        snapshot_uid = snapshots.create_snapshot(
            str(subject.subject_uid),
            manifest_uid,
            bundle_uid,
            disposition,
            ["QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"],
            [],
            max_skew_ms=_REALTIME_OBSERVATION_MAX_SKEW_MS,
            max_age_ms=_REALTIME_OBSERVATION_MAX_SKEW_MS,
        )
        snapshots.seal(snapshot_uid)
        snapshot_uids.append(snapshot_uid)
    return tuple(snapshot_uids)


def _shadow_primary_quote_statuses(
    runtime: DatabaseRuntime,
    epoch_uid: str,
    as_of: datetime,
) -> tuple[tuple[PrimaryQuoteStatus, ...], list[str], list[datetime]]:
    instant = format_rfc3339(as_of)
    fresh_after = format_rfc3339(as_of - _REALTIME_OBSERVATION_MAX_AGE)
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "WITH current_mapping AS ("
            "SELECT m.instrument_uid,m.external_code,"
            "row_number() OVER (PARTITION BY m.external_code ORDER BY m.valid_from DESC) AS rank "
            "FROM provider_mapping m JOIN instrument i ON i.instrument_uid=m.instrument_uid "
            "WHERE m.provider_key=? AND m.entity_kind='INSTRUMENT' "
            "AND m.mapping_status='RESOLVED' AND i.instrument_kind='STOCK' "
            "AND m.valid_from<=? AND (m.valid_until IS NULL OR m.valid_until>?)"
            "), primary_instruments AS ("
            "SELECT instrument_uid,(SELECT v.trading_status FROM instrument_identity_version v "
            "WHERE v.instrument_uid=current_mapping.instrument_uid AND v.valid_from<=? "
            "AND (v.valid_until IS NULL OR v.valid_until>?) "
            "ORDER BY v.valid_from DESC,v.version DESC LIMIT 1) AS trading_status "
            "FROM current_mapping WHERE rank=1"
            "), quote_candidates AS ("
            "SELECT p.instrument_uid,p.trading_status,q.quote_uid,q.source_time,q.price_status,"
            "q.price_scaled,q.volume_status,q.volume,d.pre_close_scaled,d.amount_scaled,d.quote_status,"
            "row_number() OVER (PARTITION BY p.instrument_uid ORDER BY "
            "CASE WHEN q.quote_uid IS NULL THEN 1 ELSE 0 END,q.source_time DESC,q.received_at DESC,"
            "q.quote_uid DESC) AS rank "
            "FROM primary_instruments p LEFT JOIN quote_lineage l "
            "ON l.instrument_uid=p.instrument_uid AND l.epoch_uid=? "
            "LEFT JOIN market_quote q ON q.lineage_uid=l.lineage_uid AND q.is_current=1 "
            "LEFT JOIN tdx_quote_detail d ON d.quote_uid=q.quote_uid"
            ") "
            "SELECT instrument_uid,trading_status,quote_uid,source_time,price_status,price_scaled,"
            "volume_status,volume,pre_close_scaled,amount_scaled,quote_status "
            "FROM quote_candidates WHERE rank=1 ORDER BY instrument_uid",
            (
                NativeTdxProvider.PROVIDER_KEY,
                instant,
                instant,
                instant,
                instant,
                epoch_uid,
            ),
        ).all()
    statuses: list[PrimaryQuoteStatus] = []
    quote_uids: list[str] = []
    quote_times: list[datetime] = []
    for row in rows:
        instrument_uid = str(row.instrument_uid)
        if str(row.trading_status) == "SUSPENDED":
            statuses.append(PrimaryQuoteStatus(instrument_uid, None, "SUSPENDED"))
            continue
        if row.quote_uid is None:
            statuses.append(PrimaryQuoteStatus(instrument_uid, None, "MISSING"))
            continue
        try:
            source_time = parse_rfc3339(str(row.source_time))
        except (TypeError, ValueError):
            source_time = None
        is_valid = (
            source_time is not None
            and fresh_after <= format_rfc3339(source_time) <= instant
            and str(row.price_status) == "VALUE"
            and row.price_scaled is not None
            and int(row.price_scaled) > 0
            and str(row.volume_status) == "VALUE"
            and row.volume is not None
            and int(row.volume) >= 0
            and row.pre_close_scaled is not None
            and int(row.pre_close_scaled) > 0
            and row.amount_scaled is not None
            and int(row.amount_scaled) >= 0
            and str(row.quote_status) == "VALUE"
        )
        if not is_valid:
            statuses.append(PrimaryQuoteStatus(instrument_uid, None, "NO_VALID_QUOTE"))
            continue
        assert source_time is not None
        quote_uid = str(row.quote_uid)
        statuses.append(PrimaryQuoteStatus(instrument_uid, quote_uid, "VALID"))
        quote_uids.append(quote_uid)
        quote_times.append(source_time)
    return tuple(statuses), quote_uids, quote_times


def _shadow_metric_bars(
    runtime: DatabaseRuntime,
    epoch_uid: str,
    as_of: datetime,
    same_clock_dates: Mapping[str, tuple[str, ...]],
    daily_dates: Mapping[str, tuple[str, ...]],
    *,
    minute_targets: Mapping[int, datetime] | None = None,
) -> tuple[list[str], list[str], list[datetime]]:
    if minute_targets is not None:
        return _shadow_metric_bars_for_exact_cohort(
            runtime,
            epoch_uid,
            as_of,
            same_clock_dates,
            daily_dates,
            minute_targets,
        )
    instant = format_rfc3339(as_of)
    local = as_of.astimezone(_SHANGHAI)
    day_start = format_rfc3339(
        local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    )
    complete_cutoff = format_rfc3339(as_of - timedelta(minutes=1))
    fresh_current_cutoff = format_rfc3339(
        as_of - _REALTIME_OBSERVATION_MAX_AGE - timedelta(minutes=1)
    )
    same_values = ",".join("(?,?)" for _ in range(10))
    daily_values = ",".join("(?,?)" for _ in range(40))
    same_parameters = tuple(
        value
        for exchange in ("SSE", "SZSE")
        for trading_date in same_clock_dates[exchange]
        for value in (exchange, trading_date)
    )
    daily_parameters = tuple(
        value
        for exchange in ("SSE", "SZSE")
        for trading_date in daily_dates[exchange]
        for value in (exchange, trading_date)
    )
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "WITH current_mapping AS ("
            "SELECT m.instrument_uid,m.external_code,"
            "row_number() OVER (PARTITION BY m.external_code ORDER BY m.valid_from DESC) AS rank "
            "FROM provider_mapping m JOIN instrument i ON i.instrument_uid=m.instrument_uid "
            "WHERE m.provider_key=? AND m.entity_kind='INSTRUMENT' "
            "AND m.mapping_status='RESOLVED' AND i.instrument_kind='STOCK' "
            "AND m.valid_from<=? AND (m.valid_until IS NULL OR m.valid_until>?)"
            "), primary_instruments AS ("
            "SELECT current_mapping.instrument_uid,"
            "(SELECT v.exchange FROM instrument_identity_version v "
            "WHERE v.instrument_uid=current_mapping.instrument_uid AND v.valid_from<=? "
            "AND (v.valid_until IS NULL OR v.valid_until>?) "
            "ORDER BY v.valid_from DESC,v.version DESC LIMIT 1) AS exchange "
            "FROM current_mapping WHERE rank=1"
            "), same_clock_dates(exchange,trading_date) AS (VALUES "
            + same_values
            + "), daily_dates(exchange,trading_date) AS (VALUES "
            + daily_values
            + "), current_minutes AS ("
            "SELECT b.bar_uid,b.instrument_uid,b.source_time,"
            "row_number() OVER (PARTITION BY b.instrument_uid ORDER BY b.source_time DESC) AS rank "
            "FROM tdx_bar b JOIN primary_instruments p ON p.instrument_uid=b.instrument_uid "
            "WHERE b.epoch_uid=? AND b.interval_kind='1m' AND b.source_time>=? AND b.source_time<=?"
            "), current_ends AS ("
            "SELECT instrument_uid,source_time AS current_end FROM current_minutes WHERE rank=1"
            "), fresh_current_ends AS ("
            "SELECT instrument_uid,current_end FROM current_ends WHERE current_end>=?"
            "), selected_bars AS ("
            "SELECT b.bar_uid,b.source_time,CASE WHEN b.rank=1 THEN 1 ELSE 0 END "
            "AS is_realtime_current FROM current_minutes b "
            "JOIN fresh_current_ends c ON c.instrument_uid=b.instrument_uid WHERE b.rank<=15 "
            "UNION SELECT b.bar_uid,b.source_time,0 AS is_realtime_current FROM tdx_bar b "
            "JOIN primary_instruments p ON p.instrument_uid=b.instrument_uid "
            "JOIN fresh_current_ends c ON c.instrument_uid=b.instrument_uid "
            "JOIN same_clock_dates d ON d.exchange=p.exchange "
            "WHERE b.epoch_uid=? AND b.interval_kind='1m' "
            "AND date(b.source_time,'+8 hours')=d.trading_date "
            "AND strftime('%H:%M',b.source_time,'+8 hours') "
            "BETWEEN strftime('%H:%M',c.current_end,'+8 hours','-4 minutes') "
            "AND strftime('%H:%M',c.current_end,'+8 hours') "
            "UNION SELECT b.bar_uid,b.source_time,0 AS is_realtime_current FROM tdx_bar b "
            "JOIN primary_instruments p ON p.instrument_uid=b.instrument_uid "
            "JOIN daily_dates d ON d.exchange=p.exchange "
            "WHERE b.epoch_uid=? AND b.interval_kind='1d' "
            "AND date(b.source_time,'+8 hours')=d.trading_date"
            ") SELECT bar_uid,source_time,is_realtime_current FROM selected_bars ORDER BY bar_uid",
            (
                NativeTdxProvider.PROVIDER_KEY,
                instant,
                instant,
                instant,
                instant,
                *same_parameters,
                *daily_parameters,
                epoch_uid,
                day_start,
                complete_cutoff,
                fresh_current_cutoff,
                epoch_uid,
                epoch_uid,
            ),
        ).all()
    realtime_rows = [row for row in rows if int(row.is_realtime_current) == 1]
    return (
        [str(row.bar_uid) for row in rows],
        [str(row.bar_uid) for row in realtime_rows],
        [parse_rfc3339(str(row.source_time)) + timedelta(minutes=1) for row in realtime_rows],
    )


def _shadow_metric_bars_for_exact_cohort(
    runtime: DatabaseRuntime,
    epoch_uid: str,
    as_of: datetime,
    same_clock_dates: Mapping[str, tuple[str, ...]],
    daily_dates: Mapping[str, tuple[str, ...]],
    minute_targets: Mapping[int, datetime],
) -> tuple[list[str], list[str], list[datetime]]:
    if set(minute_targets) != {0, 1} or any(
        target.tzinfo is None or target.utcoffset() is None or target + timedelta(minutes=1) > as_of
        for target in minute_targets.values()
    ):
        raise ValueError("CR-004 Shadow requires completed SSE/SZSE legal minute targets")
    instant = format_rfc3339(as_of)
    local = as_of.astimezone(_SHANGHAI)
    day_start = format_rfc3339(
        local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    )
    target_values = ",".join("(?,?)" for _ in minute_targets)
    same_values = ",".join("(?,?)" for _ in range(10))
    daily_values = ",".join("(?,?)" for _ in range(40))
    target_parameters = tuple(
        value
        for market, target in sorted(minute_targets.items())
        for value in (market, format_rfc3339(target))
    )
    same_parameters = tuple(
        value
        for exchange in ("SSE", "SZSE")
        for trading_date in same_clock_dates[exchange]
        for value in (exchange, trading_date)
    )
    daily_parameters = tuple(
        value
        for exchange in ("SSE", "SZSE")
        for trading_date in daily_dates[exchange]
        for value in (exchange, trading_date)
    )
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "WITH current_mapping AS ("
            "SELECT m.instrument_uid,m.external_code,"
            "row_number() OVER (PARTITION BY m.external_code ORDER BY m.valid_from DESC) AS rank "
            "FROM provider_mapping m JOIN instrument i ON i.instrument_uid=m.instrument_uid "
            "WHERE m.provider_key=? AND m.entity_kind='INSTRUMENT' "
            "AND m.mapping_status='RESOLVED' AND i.instrument_kind='STOCK' "
            "AND m.valid_from<=? AND (m.valid_until IS NULL OR m.valid_until>?)"
            "), primary_instruments AS ("
            "SELECT current_mapping.instrument_uid,"
            "CAST(substr(current_mapping.external_code,1,"
            "instr(current_mapping.external_code,':')-1) AS INTEGER) AS market,"
            "(SELECT v.exchange FROM instrument_identity_version v "
            "WHERE v.instrument_uid=current_mapping.instrument_uid AND v.valid_from<=? "
            "AND (v.valid_until IS NULL OR v.valid_until>?) "
            "ORDER BY v.valid_from DESC,v.version DESC LIMIT 1) AS exchange "
            "FROM current_mapping WHERE rank=1"
            "), targets(market,target_time) AS (VALUES "
            + target_values
            + "), same_clock_dates(exchange,trading_date) AS (VALUES "
            + same_values
            + "), daily_dates(exchange,trading_date) AS (VALUES "
            + daily_values
            + "), current_targets AS ("
            "SELECT b.bar_uid,b.instrument_uid,b.source_time FROM tdx_bar b "
            "JOIN primary_instruments p ON p.instrument_uid=b.instrument_uid "
            "JOIN targets t ON t.market=p.market "
            "WHERE b.epoch_uid=? AND b.interval_kind='1m' AND b.source_time=t.target_time"
            "), current_minutes AS ("
            "SELECT b.bar_uid,b.instrument_uid,b.source_time,"
            "row_number() OVER (PARTITION BY b.instrument_uid ORDER BY b.source_time DESC) AS rank "
            "FROM tdx_bar b JOIN current_targets c ON c.instrument_uid=b.instrument_uid "
            "WHERE b.epoch_uid=? AND b.interval_kind='1m' "
            "AND b.source_time>=? AND b.source_time<=c.source_time"
            "), selected_bars AS ("
            "SELECT b.bar_uid,b.source_time,"
            "CASE WHEN b.source_time=c.source_time THEN 1 ELSE 0 END AS is_realtime_current "
            "FROM current_minutes b JOIN current_targets c ON c.instrument_uid=b.instrument_uid "
            "WHERE b.rank<=15 "
            "UNION SELECT b.bar_uid,b.source_time,0 AS is_realtime_current FROM tdx_bar b "
            "JOIN primary_instruments p ON p.instrument_uid=b.instrument_uid "
            "JOIN current_targets c ON c.instrument_uid=b.instrument_uid "
            "JOIN same_clock_dates d ON d.exchange=p.exchange "
            "WHERE b.epoch_uid=? AND b.interval_kind='1m' "
            "AND date(b.source_time,'+8 hours')=d.trading_date "
            "AND strftime('%H:%M',b.source_time,'+8 hours') "
            "BETWEEN strftime('%H:%M',c.source_time,'+8 hours','-4 minutes') "
            "AND strftime('%H:%M',c.source_time,'+8 hours') "
            "UNION SELECT b.bar_uid,b.source_time,0 AS is_realtime_current FROM tdx_bar b "
            "JOIN primary_instruments p ON p.instrument_uid=b.instrument_uid "
            "JOIN daily_dates d ON d.exchange=p.exchange "
            "WHERE b.epoch_uid=? AND b.interval_kind='1d' "
            "AND date(b.source_time,'+8 hours')=d.trading_date"
            ") SELECT bar_uid,source_time,is_realtime_current FROM selected_bars ORDER BY bar_uid",
            (
                NativeTdxProvider.PROVIDER_KEY,
                instant,
                instant,
                instant,
                instant,
                *target_parameters,
                *same_parameters,
                *daily_parameters,
                epoch_uid,
                epoch_uid,
                day_start,
                epoch_uid,
                epoch_uid,
            ),
        ).all()
    realtime_rows = [row for row in rows if int(row.is_realtime_current) == 1]
    return (
        [str(row.bar_uid) for row in rows],
        [str(row.bar_uid) for row in realtime_rows],
        [parse_rfc3339(str(row.source_time)) + timedelta(minutes=1) for row in realtime_rows],
    )


def _metric_shadow_results(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    guardian_threshold_version: str | None,
    scout_threshold_version: str | None,
    iterations: int,
    snapshot_uids: Sequence[str] | None = None,
) -> list[MetricRunResult]:
    if guardian_threshold_version is None or scout_threshold_version is None:
        raise ValueError("metric Shadow threshold versions are required")
    if snapshot_uids is None:
        with runtime.read_connection() as connection:
            snapshot_uids = [
                str(row.snapshot_uid)
                for row in connection.exec_driver_sql(
                    "SELECT snapshot_uid FROM evaluation_snapshot "
                    "WHERE snapshot_status='SEALED' AND evaluation_disposition='SHADOW' "
                    "ORDER BY as_of_time,snapshot_uid"
                ).all()
            ]
    else:
        snapshot_uids = list(snapshot_uids)
    if not snapshot_uids:
        raise ValueError("--metric-shadow requires sealed CR-004 SHADOW snapshots")
    runner = MetricRunner(runtime, writer, artifacts)
    results: list[MetricRunResult] = []
    for _ in range(iterations):
        for snapshot_uid in snapshot_uids:
            result = runner.run(
                snapshot_uid,
                guardian_threshold_version=guardian_threshold_version,
                scout_threshold_version=scout_threshold_version,
            )
            results.append(result)
    return results


def _run_metric_shadow(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    guardian_threshold_version: str | None,
    scout_threshold_version: str | None,
    iterations: int,
) -> list[dict[str, Any]]:
    return [
        _metric_run_record(result)
        for result in _metric_shadow_results(
            runtime,
            writer,
            artifacts,
            guardian_threshold_version,
            scout_threshold_version,
            iterations,
        )
    ]


def _metric_run_record(result: MetricRunResult) -> dict[str, Any]:
    return {
        "snapshot_uid": result.snapshot_uid,
        "elapsed_ms": result.elapsed_ms,
        "metric_evaluation_ms": result.metric_evaluation_ms,
        "threshold_evaluation_ms": result.threshold_evaluation_ms,
        "evidence_persistence_ms": result.evidence_persistence_ms,
        "fitness_status": result.quality.fitness_status,
        "guardian_matches": list(result.guardian_threshold_matches),
        "scout_matches": list(result.scout_threshold_matches),
        "evidence_sha256": result.evidence_sha256,
    }


def _shadow_calibration_report(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    metric_results: Sequence[MetricRunResult],
    guardian_threshold_version: str | None,
    scout_threshold_version: str | None,
    observed_at: datetime,
) -> dict[str, Any]:
    if guardian_threshold_version is None or scout_threshold_version is None:
        raise ValueError("metric Shadow threshold versions required")
    result = CalibrationRunner(artifacts, ThresholdRegistry(runtime, writer)).evaluate(
        metric_results,
        guardian_version=guardian_threshold_version,
        scout_version=scout_threshold_version,
        validation_kind="SHADOW",
        observed_at=observed_at,
    )
    return {
        "evidence_sha256": result.evidence_sha256,
        "validation_kind": result.validation_kind,
        "metric_run_count": result.metric_run_count,
        "guardian_threshold_version": result.guardian_threshold_version,
        "scout_threshold_version": result.scout_threshold_version,
        "producer_versions": list(result.producer_versions),
        "guardian_trigger_counts": dict(result.guardian_trigger_counts),
        "scout_trigger_counts": dict(result.scout_trigger_counts),
        "guardian_effect_counts": dict(result.guardian_effect_counts),
        "guardian_suppression_count": result.guardian_suppression_count,
        "guardian_downgrade_count": result.guardian_downgrade_count,
        "guardian_pause_count": result.guardian_pause_count,
        "quality_counts": dict(result.quality_counts),
        "phase_counts": dict(result.phase_counts),
        "data_limitation_count": result.data_limitation_count,
        "unfit_count": result.unfit_count,
        "conflicts": list(result.conflicts),
        "guardian_never_triggered": list(result.guardian_never_triggered),
        "scout_never_triggered": list(result.scout_never_triggered),
        "guardian_stressed_sample_counts": dict(result.guardian_stressed_sample_counts),
        "scout_strong_broad_sample_counts": dict(result.scout_strong_broad_sample_counts),
        "guardian_never_on_stressed": list(result.guardian_never_on_stressed),
        "scout_never_on_strong_broad": list(result.scout_never_on_strong_broad),
        "failure_codes": list(result.failure_codes),
        "passed": result.passed,
    }


def _representative_probes(gateway: TdxGateway, observed_at: datetime) -> dict[str, bool]:
    probe = getattr(gateway, "representative_probe", None)
    if not callable(probe):
        return {}
    result = probe(observed_at)
    if not isinstance(result, Mapping):
        raise ValueError("TDX representative probe result must be a mapping")
    return {str(name): bool(value) for name, value in result.items()}


def _side_effect_counts(runtime: DatabaseRuntime) -> dict[str, int]:
    with runtime.read_connection() as connection:
        return {
            table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
            for table in _SIDE_EFFECT_TABLES
        }


def _audit_surface_counts(runtime: DatabaseRuntime) -> dict[str, dict[str, int]]:
    with runtime.read_connection() as connection:
        return {
            "official_business": {
                name: int(connection.exec_driver_sql(query).scalar_one())
                for name, query in _OFFICIAL_BUSINESS_AUDIT_QUERIES.items()
            },
            "reference_onboarding": {
                name: int(connection.exec_driver_sql(query).scalar_one())
                for name, query in _REFERENCE_ONBOARDING_AUDIT_QUERIES.items()
            },
        }


def _side_effect_audit(
    before: Mapping[str, Mapping[str, int]], after: Mapping[str, Mapping[str, int]]
) -> dict[str, Any]:
    official_before = before["official_business"]
    official_after = after["official_business"]
    reference_before = before["reference_onboarding"]
    reference_after = after["reference_onboarding"]
    if set(official_before) != set(official_after) or set(reference_before) != set(reference_after):
        raise ValueError("Shadow audit surface changed during run")
    official_delta = {
        name: int(official_after[name]) - int(official_before[name])
        for name in sorted(official_before)
    }
    reference_delta = {
        name: int(reference_after[name]) - int(reference_before[name])
        for name in sorted(reference_before)
    }
    return {
        "schema_version": 1,
        "passed": all(value == 0 for value in official_delta.values()),
        "official_business_side_effect_delta": sum(official_delta.values()),
        "official_business": {
            "before": dict(official_before),
            "after": dict(official_after),
            "delta": official_delta,
        },
        "reference_onboarding": {
            "classification": "NON_BUSINESS_REFERENCE",
            "before": dict(reference_before),
            "after": dict(reference_after),
            "delta": reference_delta,
        },
    }


def _evidence_counts(runtime: DatabaseRuntime, epoch_uid: str) -> dict[str, int]:
    with runtime.read_connection() as connection:
        return {
            "market_data_batches": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM market_data_batch WHERE epoch_uid=?", (epoch_uid,)
                ).scalar_one()
            ),
            "raw_records": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM raw_market_record r JOIN market_data_batch b "
                    "ON b.batch_uid=r.batch_uid WHERE b.epoch_uid=?",
                    (epoch_uid,),
                ).scalar_one()
            ),
            "quotes": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM market_quote q JOIN market_data_batch b "
                    "ON b.batch_uid=q.batch_uid WHERE b.epoch_uid=?",
                    (epoch_uid,),
                ).scalar_one()
            ),
            "quote_details": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM tdx_quote_detail d JOIN market_quote q "
                    "ON q.quote_uid=d.quote_uid JOIN market_data_batch b "
                    "ON b.batch_uid=q.batch_uid "
                    "WHERE b.epoch_uid=?",
                    (epoch_uid,),
                ).scalar_one()
            ),
            "bars": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM tdx_bar WHERE epoch_uid=?", (epoch_uid,)
                ).scalar_one()
            ),
            "block_versions": int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM tdx_block_artifact_version WHERE epoch_uid=?",
                    (epoch_uid,),
                ).scalar_one()
            ),
        }


def _freshness(runtime: DatabaseRuntime, epoch_uid: str) -> dict[str, str | None]:
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT max(q.received_at) AS received_at,max(q.source_time) AS source_time "
            "FROM market_quote q JOIN market_data_batch b ON b.batch_uid=q.batch_uid "
            "WHERE b.epoch_uid=?",
            (epoch_uid,),
        ).one()
    return {"received_at": row.received_at, "source_time": row.source_time}


def _advance_bar_watermarks(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    epoch_uid: str,
    received_at: datetime,
) -> dict[str, dict[str, str | int | bool | None]]:
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "SELECT interval_kind,max(source_time) AS source_time FROM tdx_bar "
            "WHERE epoch_uid=? GROUP BY interval_kind",
            (epoch_uid,),
        ).all()
    capabilities = {"1m": "MINUTE_BARS", "1d": "DAILY_BARS"}
    repository = WatermarkRepository(runtime, writer)
    result: dict[str, dict[str, str | int | bool | None]] = {}
    for row in rows:
        capability = capabilities.get(str(row.interval_kind))
        if capability is None or row.source_time is None:
            continue
        watermark = repository.advance(
            epoch_uid,
            capability,
            parse_rfc3339(str(row.source_time)),
            received_at,
        )
        result[capability] = {
            "event_time": watermark.event_time,
            "received_time": watermark.received_time,
            "version": watermark.version,
            "rewarm_required": watermark.rewarm_required,
        }
    return result


def _transport_report(
    node_health: Callable[[], Sequence[object]] | None,
    transport_statistics: Callable[[], object] | None,
) -> dict[str, Any]:
    nodes = () if node_health is None else node_health()
    statistics = None if transport_statistics is None else transport_statistics()
    return {
        "requests": int(getattr(statistics, "requests", 0)),
        "failed_attempts": int(getattr(statistics, "failed_attempts", 0)),
        "failovers": int(getattr(statistics, "failovers", 0)),
        "recoveries": int(getattr(statistics, "recoveries", 0)),
        "nodes": [_node_report(node) for node in nodes],
    }


def _node_report(node: object) -> dict[str, Any]:
    lane: str | None = None
    if isinstance(node, tuple) and len(node) == 2 and isinstance(node[0], str):
        lane = node[0]
        node = node[1]
    result: dict[str, Any] = {
        "host": str(getattr(node, "host")),
        "port": int(getattr(node, "port")),
        "state": str(getattr(node, "state")),
        "connect_latency_ms": getattr(node, "connect_latency_ms"),
        "request_latency_ms": getattr(node, "request_latency_ms"),
        "consecutive_failures": int(getattr(node, "consecutive_failures")),
        "successful_probes": list(getattr(node, "successful_probes")),
    }
    if lane is not None:
        result["connection_lane"] = lane
    return result


def _combined_pool_statistics(pools: Sequence[TdxServerPool]) -> TdxPoolStatistics:
    statistics = [pool.statistics() for pool in pools]
    return TdxPoolStatistics(
        sum(item.requests for item in statistics),
        sum(item.failed_attempts for item in statistics),
        sum(item.failovers for item in statistics),
        sum(item.recoveries for item in statistics),
    )


def _labeled_pool_health(pools: Sequence[TdxServerPool]) -> tuple[tuple[str, object], ...]:
    return tuple(
        ("primary" if index == 0 else f"minute-{index}", node)
        for index, pool in enumerate(pools)
        for node in pool.health_snapshot()
    )


def _percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 0
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be between 1 and 100")
    ordered = sorted(values)
    return ordered[(len(ordered) * percentile - 1) // 100]


def metric_timing_summary(durations_ms: Sequence[int]) -> dict[str, int | list[int]]:
    """Format CR-004 full-metric timing with the existing TDX percentile policy."""
    durations = list(durations_ms)
    return {
        "count": len(durations),
        "durations_ms": durations,
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "max_ms": max(durations, default=0),
    }


def _shadow_timing_summary(
    rounds: Sequence[Mapping[str, Any]],
    metric_results: Sequence[MetricRunResult],
    calibration_elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "fresh_round": metric_timing_summary([int(round_["elapsed_ms"]) for round_ in rounds]),
        "stages": {
            "acquisition": metric_timing_summary(
                [int(round_["acquisition_elapsed_ms"]) for round_ in rounds]
            ),
            "quote_acquisition": metric_timing_summary(
                [int(round_["quote_acquisition_elapsed_ms"]) for round_ in rounds]
            ),
            "minute_cohort_acquisition": metric_timing_summary(
                [int(round_["minute_cohort_elapsed_ms"]) for round_ in rounds]
            ),
            "input_snapshot_preparation": metric_timing_summary(
                [int(round_["preparation_elapsed_ms"]) for round_ in rounds]
            ),
            "metric_evaluation": metric_timing_summary(
                [result.metric_evaluation_ms for result in metric_results]
            ),
            "threshold_evaluation": metric_timing_summary(
                [result.threshold_evaluation_ms for result in metric_results]
            ),
            "metric_evidence_persistence": metric_timing_summary(
                [result.evidence_persistence_ms for result in metric_results]
            ),
            "calibration_evidence": metric_timing_summary([calibration_elapsed_ms]),
        },
    }


def _parse_server(value: str) -> TdxServer:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise ValueError("TDX server must be host:port")
    return TdxServer(host, int(port))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Native TDX acquisition without analysis or notification"
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sweeps", type=int, default=1)
    parser.add_argument("--server", action="append", default=[])
    parser.add_argument("--metric-shadow", action="store_true")
    parser.add_argument("--guardian-threshold-version")
    parser.add_argument("--scout-threshold-version")
    parser.add_argument(
        "--repeatability-iterations",
        "--metric-iterations",
        dest="metric_iterations",
        type=int,
        default=1,
        help="repeat one sealed Shadow round for determinism; never acceptance-round count",
    )
    parser.add_argument("--trading-calendar-file", type=Path)
    parser.add_argument("--listing-reference-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.once and arguments.sweeps != 1:
        _parser().error("--once cannot be combined with a sweep count other than one")
    if arguments.metric_shadow and (
        not arguments.guardian_threshold_version or not arguments.scout_threshold_version
    ):
        _parser().error("--metric-shadow requires both explicit threshold version arguments")
    if arguments.metric_iterations < 1:
        _parser().error("--repeatability-iterations must be positive")
    protocol = TdxProtocol()
    servers = tuple(_parse_server(item) for item in arguments.server) or DEFAULT_SERVERS
    pool = TdxServerPool(servers, protocol, required_probes=REPRESENTATIVE_PROBES)
    minute_pools: list[TdxServerPool] = []
    try:
        if arguments.metric_shadow:
            minute_pools = [TdxServerPool(servers, protocol) for _ in range(4)]
        pools = (pool, *minute_pools)
        report = run_shadow(
            arguments.data_dir,
            TdxLiveClient(pool, protocol),
            sweeps=arguments.sweeps,
            metric_shadow=arguments.metric_shadow,
            guardian_threshold_version=arguments.guardian_threshold_version,
            scout_threshold_version=arguments.scout_threshold_version,
            metric_iterations=arguments.metric_iterations,
            trading_calendar_file=arguments.trading_calendar_file,
            listing_reference_file=arguments.listing_reference_file,
            node_health=lambda: _labeled_pool_health(pools),
            transport_statistics=lambda: _combined_pool_statistics(pools),
            minute_gateways=tuple(TdxLiveClient(item, protocol) for item in minute_pools),
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        for item in (pool, *minute_pools):
            item.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
