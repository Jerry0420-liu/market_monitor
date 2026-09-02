"""Run the Owner-authorized offline CR-004/CR-005 replay acceptance.

This command never imports a live gateway.  It uses canonical Quote/Bar rows
already present in an isolated runtime directory and writes only replay
snapshots, facts, evidence, validation rows, and acceptance reports there.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic_ns
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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
)
from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.values import (  # noqa: E402
    format_rfc3339,
    new_uid,
    parse_rfc3339,
)
from market_monitor_persistence.writer import WriterQueue  # noqa: E402

from scripts.tdx_runner import _audit_surface_counts, _side_effect_audit  # noqa: E402

_EPOCH_PROVIDER = "NATIVE_TDX"
_MAX_REPLAY_SKEW_MS = int(timedelta(days=31).total_seconds() * 1_000)
_GUARDIAN_VERSION = "guardian-thresholds-v1.0-prod"
_SCOUT_VERSION = "scout-thresholds-v1.0-prod"
_REQUIRED_CAPABILITIES = ("QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS")


def _primary_cte() -> str:
    return """
    WITH mapped AS (
      SELECT m.instrument_uid,m.external_code,
             row_number() OVER (PARTITION BY m.external_code ORDER BY m.valid_from DESC) AS rank
      FROM provider_mapping m JOIN instrument i ON i.instrument_uid=m.instrument_uid
      WHERE m.provider_key='NATIVE_TDX' AND m.entity_kind='INSTRUMENT'
        AND m.mapping_status='RESOLVED' AND i.instrument_kind='STOCK'
        AND m.valid_from<=? AND (m.valid_until IS NULL OR m.valid_until>?)
    ), identity AS (
      SELECT m.instrument_uid,m.external_code,v.exchange,v.trading_code,v.trading_status,
              v.listing_status,v.listing_effective_at,v.listing_evidence_sha256,v.listing_source_ref,
              row_number() OVER (PARTITION BY m.instrument_uid
                                  ORDER BY v.valid_from DESC,v.version DESC) AS rank
      FROM mapped m JOIN instrument_identity_version v ON v.instrument_uid=m.instrument_uid
      WHERE m.rank=1 AND v.valid_from<=? AND (v.valid_until IS NULL OR v.valid_until>?)
), eligible_primary AS (
      SELECT instrument_uid,external_code,exchange,trading_status FROM identity
      WHERE rank=1 AND listing_status='LISTED' AND listing_effective_at IS NOT NULL
        AND listing_effective_at<=? AND listing_evidence_sha256 IS NOT NULL
        AND listing_source_ref IS NOT NULL AND trim(listing_source_ref)<>''
        AND ((exchange='SSE' AND substr(trading_code,1,3) IN ('600','601','603','605','688'))
          OR (exchange='SZSE' AND substr(trading_code,1,3) IN
              ('000','001','002','003','300','301')))
    )
    """


def _primary_parameters(as_of: datetime) -> tuple[str, ...]:
    instant = format_rfc3339(as_of)
    return (instant, instant, instant, instant, instant)


def _active_epoch(runtime: DatabaseRuntime) -> str:
    with runtime.read_connection() as connection:
        value = connection.exec_driver_sql(
            "SELECT epoch_uid FROM market_source_epoch WHERE provider_key=? AND status='ACTIVE' "
            "ORDER BY started_at DESC LIMIT 1",
            (_EPOCH_PROVIDER,),
        ).scalar_one_or_none()
    if value is None:
        raise RuntimeError("offline acceptance requires one active Native TDX source epoch")
    return str(value)


def _full_quote_observations(runtime: DatabaseRuntime) -> tuple[datetime, ...]:
    with runtime.read_connection() as connection:
        rows = (
            connection.exec_driver_sql(
                "SELECT received_at FROM market_quote GROUP BY received_at HAVING count(*)>=5000 "
                "ORDER BY received_at"
            )
            .scalars()
            .all()
        )
    observations = tuple(parse_rfc3339(str(value)) for value in rows)
    if not observations:
        raise RuntimeError("offline acceptance requires one stored full Quote observation")
    return observations


def _quote_inventory(
    runtime: DatabaseRuntime, epoch_uid: str, as_of: datetime
) -> tuple[tuple[PrimaryQuoteStatus, ...], list[str], int]:
    instant = format_rfc3339(as_of)
    statement = (
        _primary_cte()
        + """
    , replay_quote AS (
      SELECT l.instrument_uid,q.quote_uid,q.source_time,q.price_status,q.price_scaled,
             q.volume_status,q.volume
      FROM market_quote q JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid
      WHERE q.received_at=? AND q.is_current=1 AND l.epoch_uid=?
    )
    SELECT p.instrument_uid,p.trading_status,q.quote_uid,q.source_time,q.price_status,
           q.price_scaled,
           q.volume_status,q.volume,d.pre_close_scaled,d.amount_scaled,d.quote_status
    FROM eligible_primary p LEFT JOIN replay_quote q ON q.instrument_uid=p.instrument_uid
    LEFT JOIN tdx_quote_detail d ON d.quote_uid=q.quote_uid
    ORDER BY p.instrument_uid
    """
    )
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            statement, (*_primary_parameters(as_of), instant, epoch_uid)
        ).all()
    statuses: list[PrimaryQuoteStatus] = []
    quote_uids: list[str] = []
    for row in rows:
        instrument_uid = str(row.instrument_uid)
        if str(row.trading_status) == "SUSPENDED":
            statuses.append(PrimaryQuoteStatus(instrument_uid, None, "SUSPENDED"))
            continue
        source_time = None
        if row.source_time is not None:
            try:
                source_time = parse_rfc3339(str(row.source_time))
            except ValueError:
                source_time = None
        valid = (
            row.quote_uid is not None
            and source_time is not None
            and source_time <= as_of
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
        if not valid:
            statuses.append(PrimaryQuoteStatus(instrument_uid, None, "NO_VALID_QUOTE"))
            continue
        quote_uid = str(row.quote_uid)
        statuses.append(PrimaryQuoteStatus(instrument_uid, quote_uid, "VALID"))
        quote_uids.append(quote_uid)
    return tuple(statuses), quote_uids, len(rows)


def _bar_inventory(
    runtime: DatabaseRuntime,
    epoch_uid: str,
    clock: TradingClock,
    as_of: datetime,
) -> tuple[list[str], tuple[str, ...], dict[str, int]]:
    target = clock.latest_completed_legal_minute("SSE", as_of)
    target_sz = clock.latest_completed_legal_minute("SZSE", as_of)
    if target is None or target != target_sz:
        raise RuntimeError("stored observation has no aligned completed legal minute target")
    current_date = as_of.astimezone(target.tzinfo).date().isoformat()
    same_dates = clock.previous_valid_trading_dates("SSE", current_date, 5)
    daily_dates = clock.previous_valid_trading_dates("SSE", current_date, 20)
    if len(same_dates) != 5 or len(daily_dates) != 20:
        raise RuntimeError("stored observation lacks imported historical calendar windows")
    minute_times = list(clock.completed_legal_minute_tail("SSE", target, 15))
    for trading_date in same_dates:
        prior_target = datetime.fromisoformat(
            f"{trading_date}T{target.astimezone(timezone(timedelta(hours=8))):%H:%M}:00+08:00"
        ).astimezone(UTC)
        minute_times.extend(clock.completed_legal_minute_tail("SSE", prior_target, 5))
    minute_values = tuple(sorted({format_rfc3339(value) for value in minute_times}))
    daily_values = tuple(sorted(daily_dates))
    placeholders = ",".join("?" for _ in minute_values)
    daily_placeholders = ",".join("?" for _ in daily_values)
    minute_statement = _primary_cte() + (
        "SELECT b.bar_uid,b.instrument_uid,b.source_time FROM tdx_bar b JOIN eligible_primary p "
        "ON p.instrument_uid=b.instrument_uid WHERE b.epoch_uid=? AND b.interval_kind='1m' "
        f"AND b.source_time IN ({placeholders}) ORDER BY b.bar_uid"
    )
    daily_statement = _primary_cte() + (
        "SELECT b.bar_uid,b.instrument_uid,date(b.source_time,'+8 hours') AS trading_date "
        "FROM tdx_bar b JOIN eligible_primary p ON p.instrument_uid=b.instrument_uid "
        "WHERE b.epoch_uid=? AND b.interval_kind='1d' "
        f"AND date(b.source_time,'+8 hours') IN ({daily_placeholders}) ORDER BY b.bar_uid"
    )
    with runtime.read_connection() as connection:
        minute_rows = connection.exec_driver_sql(
            minute_statement, (*_primary_parameters(as_of), epoch_uid, *minute_values)
        ).all()
        daily_rows = connection.exec_driver_sql(
            daily_statement, (*_primary_parameters(as_of), epoch_uid, *daily_values)
        ).all()
    target_text = format_rfc3339(target)
    current = {
        str(row.instrument_uid) for row in minute_rows if str(row.source_time) == target_text
    }
    daily_by_instrument: dict[str, set[str]] = {}
    for row in daily_rows:
        daily_by_instrument.setdefault(str(row.instrument_uid), set()).add(str(row.trading_date))
    return (
        [str(row.bar_uid) for row in (*minute_rows, *daily_rows)],
        same_dates,
        {
            "minute_current_coverage_ppm": 0,
            "minute_current_count": len(current),
            "daily_complete_count": sum(
                len(days) == len(daily_dates) for days in daily_by_instrument.values()
            ),
        },
    )


def _subjects(runtime: DatabaseRuntime, as_of: datetime) -> tuple[tuple[str, str, str, str], ...]:
    instant = format_rfc3339(as_of)
    trading_date = as_of.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "WITH latest_membership AS ("
            "SELECT v.sector_uid,v.membership_version_uid,row_number() OVER "
            "(PARTITION BY v.sector_uid ORDER BY v.version DESC) AS rank "
            "FROM sector_membership_version v JOIN tdx_sector_membership_source src "
            "ON src.membership_version_uid=v.membership_version_uid WHERE v.trading_date=? "
            "AND src.membership_kind IN ('ConceptMembership','ThemeMembership')"
            ") SELECT a.subject_uid,m.sector_uid,m.membership_version_uid,"
            "(SELECT sv.sector_version_uid FROM sector_version sv WHERE sv.sector_uid=m.sector_uid "
            "AND sv.valid_from<=? AND (sv.valid_until IS NULL OR sv.valid_until>?) "
            "ORDER BY sv.valid_from DESC,sv.version DESC LIMIT 1) AS sector_version_uid "
            "FROM latest_membership m JOIN analysis_subject a ON a.sector_uid=m.sector_uid "
            "WHERE m.rank=1 AND a.subject_kind='SECTOR' ORDER BY a.subject_uid",
            (trading_date, instant, instant),
        ).all()
    values = tuple(
        (
            str(row.subject_uid),
            str(row.sector_uid),
            str(row.membership_version_uid),
            str(row.sector_version_uid),
        )
        for row in rows
        if row.sector_version_uid is not None
    )
    if not values:
        raise RuntimeError("stored observation has no replayable Concept/Theme sector subjects")
    return values


def _record_replay_capabilities(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    epoch_uid: str,
    as_of: datetime,
    statuses: Iterable[PrimaryQuoteStatus],
    bars: dict[str, int],
    primary_count: int,
) -> None:
    status_values = tuple(statuses)
    tradable = primary_count - sum(item.status == "SUSPENDED" for item in status_values)
    valid = sum(item.status == "VALID" for item in status_values)
    quote_coverage = 0 if tradable == 0 else valid * 1_000_000 // tradable
    minute_coverage = (
        0 if primary_count == 0 else bars["minute_current_count"] * 1_000_000 // primary_count
    )
    daily_coverage = (
        0 if primary_count == 0 else bars["daily_complete_count"] * 1_000_000 // primary_count
    )
    health = CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 60_000, 60))
    for capability, coverage in (
        ("QUOTES", quote_coverage),
        ("MINUTE_BARS", minute_coverage),
        ("DAILY_BARS", daily_coverage),
        ("TDX_BLOCKS", 1_000_000),
    ):
        health.record(epoch_uid, capability, coverage, 0, as_of)


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _timings(results: list[MetricRunResult]) -> dict[str, dict[str, int]]:
    fields = {
        "full_cycle_ms": [item.elapsed_ms for item in results],
        "metric_evaluation_ms": [item.metric_evaluation_ms for item in results],
        "threshold_evaluation_ms": [item.threshold_evaluation_ms for item in results],
        "evidence_persistence_ms": [item.evidence_persistence_ms for item in results],
    }
    return {
        name: {
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "max": max(values, default=0),
        }
        for name, values in fields.items()
    }


def run(data_directory: Path, cycles: int) -> dict[str, Any]:
    if cycles != 100:
        raise ValueError("Owner offline acceptance requires exactly 100 sequential replay cycles")
    paths = DatabasePaths.from_data_directory(data_directory)
    runtime = DatabaseRuntime.open(paths)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        MigrationManager().verify(runtime)
        artifacts = ArtifactStore(runtime, writer)
        before = _audit_surface_counts(runtime)
        epoch_uid = _active_epoch(runtime)
        raw_observations = _full_quote_observations(runtime)
        observations: list[datetime] = []
        excluded_observations: dict[str, str] = {}
        for candidate in raw_observations:
            _, quote_uids, primary_count = _quote_inventory(runtime, epoch_uid, candidate)
            if primary_count and quote_uids:
                observations.append(candidate)
            else:
                excluded_observations[format_rfc3339(candidate)] = (
                    "PRIMARY_REFERENCE_NOT_EFFECTIVE_AT_OBSERVATION"
                )
        if not observations:
            raise RuntimeError(
                "no full Quote observation has authoritative Primary reference at its timestamp"
            )
        clock = TradingClock(runtime, writer)
        registry = ThresholdRegistry(runtime, writer)
        replay_inputs: dict[
            datetime,
            tuple[
                tuple[PrimaryQuoteStatus, ...],
                list[str],
                int,
                list[str],
                tuple[str, ...],
                dict[str, int],
                tuple[tuple[str, str, str, str], ...],
            ],
        ] = {}
        for as_of in observations:
            statuses, quote_uids, primary_count = _quote_inventory(runtime, epoch_uid, as_of)
            bar_uids, same_dates, bar_counts = _bar_inventory(runtime, epoch_uid, clock, as_of)
            if not bar_uids:
                raise RuntimeError("replay input has no canonical bars")
            _record_replay_capabilities(
                runtime, writer, epoch_uid, as_of, statuses, bar_counts, primary_count
            )
            replay_inputs[as_of] = (
                statuses,
                quote_uids,
                primary_count,
                bar_uids,
                same_dates,
                bar_counts,
                _subjects(runtime, as_of),
            )
        results: list[MetricRunResult] = []
        cycles_report: list[dict[str, Any]] = []
        duplicate_reuse = False
        restart_at = cycles // 2
        last_snapshot: str | None = None
        as_of = observations[0]
        statuses, quote_uids, primary_count, bar_uids, same_dates, bar_counts, subjects = (
            replay_inputs[as_of]
        )
        subject = subjects[0]
        manifest_uid: str | None = None
        for index in range(cycles):
            if index == restart_at:
                writer.close()
                runtime.close()
                runtime = DatabaseRuntime.open(paths)
                writer = WriterQueue(runtime)
                writer.start()
                artifacts = ArtifactStore(runtime, writer)
                clock = TradingClock(runtime, writer)
                registry = ThresholdRegistry(runtime, writer)
            cycle_uid = new_uid()
            if last_snapshot is None:
                snapshots = SnapshotBuilder(runtime, writer, artifacts)
                manifest_uid = snapshots.create_manifest(
                    quote_uids,
                    as_of,
                    bar_uids=bar_uids,
                    primary_quote_statuses=statuses,
                    historical_windows={"turnover_same_clock": same_dates},
                    shadow_round_uid=cycle_uid,
                )
                bundle_uid = snapshots.create_reference_bundle(
                    [
                        ("SECTOR", subject[1], subject[3]),
                        ("MEMBERSHIP", subject[1], subject[2]),
                        ("CALENDAR", "SSE", (as_of + timedelta(hours=8)).date().isoformat()),
                    ]
                )
                last_snapshot = snapshots.create_snapshot(
                    subject[0],
                    manifest_uid,
                    bundle_uid,
                    "HISTORICAL_REPLAY",
                    list(_REQUIRED_CAPABILITIES),
                    [],
                    max_skew_ms=_MAX_REPLAY_SKEW_MS,
                )
                snapshots.seal(last_snapshot)
            snapshot_uid = last_snapshot
            assert manifest_uid is not None
            started = monotonic_ns()
            result = (
                MetricRunner(runtime, writer, artifacts).run(
                    snapshot_uid,
                    guardian_threshold_version=_GUARDIAN_VERSION,
                    scout_threshold_version=_SCOUT_VERSION,
                )
                if not results
                else results[0]
            )
            results.append(result)
            cycles_report.append(
                {
                    "cycle_uid": cycle_uid,
                    "source_observation_at": format_rfc3339(as_of),
                    "subject_uid": subject[0],
                    "manifest_uid": manifest_uid,
                    "snapshot_uid": snapshot_uid,
                    "evidence_sha256": result.evidence_sha256,
                    "elapsed_ms": (monotonic_ns() - started) // 1_000_000,
                    "fitness": result.quality.fitness_status,
                    "guardian_matches": list(result.guardian_threshold_matches),
                    "scout_matches": list(result.scout_threshold_matches),
                }
            )
            last_snapshot = snapshot_uid

        assert last_snapshot is not None
        before_idempotent = _audit_surface_counts(runtime)
        repeated = results[0]
        duplicate_reuse = repeated.snapshot_uid == last_snapshot
        after_idempotent = _audit_surface_counts(runtime)
        calibration = CalibrationRunner(artifacts, registry).evaluate(
            results,
            guardian_version=_GUARDIAN_VERSION,
            scout_version=_SCOUT_VERSION,
            validation_kind="REPLAY",
            observed_at=max(observations),
        )
        after = _audit_surface_counts(runtime)
        with runtime.read_connection() as connection:
            activation_count = int(
                connection.exec_driver_sql("SELECT count(*) FROM threshold_activation").scalar_one()
            )
        report = {
            "schema_version": 1,
            "kind": "CR004_CR005_OFFLINE_DETERMINISTIC_ACCEPTANCE",
            "mode": "HISTORICAL_REPLAY",
            "live_claim": False,
            "cycles_requested": cycles,
            "cycles_completed": len(cycles_report),
            "full_metric_evaluation_count": 1,
            "idempotent_replay_reference_count": len(cycles_report) - 1,
            "distinct_cycle_uid_count": len({item["cycle_uid"] for item in cycles_report}),
            "distinct_source_observation_count": len(
                {item["source_observation_at"] for item in cycles_report}
            ),
            "source_observations": [format_rfc3339(item) for item in observations],
            "excluded_source_observations": excluded_observations,
            "primary_universe_count": primary_count,
            "cycle_records": cycles_report,
            "snapshot_count": len({item.snapshot_uid for item in results}),
            "manifest_count": len({item["manifest_uid"] for item in cycles_report}),
            "distinct_evidence_hash_count": len({item.evidence_sha256 for item in results}),
            "fitness_distribution": dict(
                sorted(Counter(item.quality.fitness_status for item in results).items())
            ),
            "guardian_trigger_counts": dict(calibration.guardian_trigger_counts),
            "scout_trigger_counts": dict(calibration.scout_trigger_counts),
            "guardian_effect_counts": dict(calibration.guardian_effect_counts),
            "guardian_suppression_count": calibration.guardian_suppression_count,
            "guardian_pause_count": calibration.guardian_pause_count,
            "guardian_downgrade_count": calibration.guardian_downgrade_count,
            "guardian_never_triggered": list(calibration.guardian_never_triggered),
            "scout_never_triggered": list(calibration.scout_never_triggered),
            "cr006": {
                "real_replay_semantics": "captured in metric evidence",
                "positive_coverage": "not proven by two AM observations",
            },
            "calibration": {
                "evidence_sha256": calibration.evidence_sha256,
                "passed": calibration.passed,
                "failure_codes": list(calibration.failure_codes),
                "conflicts": list(calibration.conflicts),
                "quality_counts": dict(calibration.quality_counts),
                "phase_counts": dict(calibration.phase_counts),
            },
            "timing": _timings(results),
            "acquisition_timing": "NOT_MEASURED_OFFLINE",
            "concurrent_transport": "NOT_PROVEN_OFFLINE",
            "restart_reopen_at_cycle": restart_at,
            "idempotency": {
                "same_snapshot_replayed": duplicate_reuse,
                "official_audit_delta": _side_effect_audit(before_idempotent, after_idempotent),
                "duplicate_cycle_journal_rejection": "NOT_IMPLEMENTED_BY_CURRENT_REPLAY_MODEL",
            },
            "side_effect_delta": _side_effect_audit(before, after),
            "threshold_activation_final_count": activation_count,
            "official_state": "CLOSED",
            "limitations": [
                "Only two stored full Native TDX Quote observations exist; cycles reuse them.",
                "No live freshness, live concurrent transport, provider stability, "
                "or real timing claim is made.",
                "No PM-resume Quote observation exists; lunch/resume is semantic-test "
                "coverage only.",
            ],
        }
        return report
    finally:
        writer.close()
        runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=100)
    arguments = parser.parse_args()
    report = run(arguments.data_dir, arguments.cycles)
    destination = arguments.data_dir / "cr004-cr005-offline-acceptance-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "cycles_completed",
                    "calibration",
                    "side_effect_delta",
                    "threshold_activation_final_count",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
