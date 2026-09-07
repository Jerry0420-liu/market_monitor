"""Offline CR-004 core-chain checks for current 1m right-edge semantics."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from market_monitor_analysis.guardian import GuardianService
from market_monitor_analysis.metric_runner import MetricRunner
from market_monitor_analysis.scout import ScoutService
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_analysis.state import StateService
from market_monitor_data.clock import TradingClock
from market_monitor_data.health import CapabilityHealthService, HealthThresholds
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import (
    TdxBar,
    TdxBlockArtifact,
    TdxBlockMembership,
    TdxInstrument,
    TdxQuoteDetail,
)
from market_monitor_data.tdx.provider import TdxMinuteIncrementResult
from market_monitor_data.tdx.storage import TdxStorage

from scripts.tdx_runner import _audit_surface_counts, _prepare_metric_shadow_snapshots
from tests.cr004.conftest import _CODES, _SAME_CLOCK_DATES, _bar
from tests.cr004.test_metric_performance import _CALENDAR_DATES

_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_TRADING_DATE = "2026-08-24"
_SZSE_CODES = ("000001", "000002")
_SESSIONS = (
    ("CONTINUOUS_AM", "01:30:00Z", "03:30:00Z"),
    ("CONTINUOUS_PM", "05:00:00Z", "07:00:00Z"),
)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _ensure_calendars(runtime: Any, writer: Any) -> None:
    clock = TradingClock(runtime, writer)
    with runtime.read_connection() as connection:
        existing_days = {
            (str(row.exchange), str(row.trading_date))
            for row in connection.exec_driver_sql(
                "SELECT exchange,trading_date FROM trading_calendar_day"
            ).all()
        }
    for exchange in ("SSE", "SZSE"):
        for trading_date in _CALENDAR_DATES:
            if (exchange, trading_date) not in existing_days:
                clock.import_day(
                    exchange,
                    trading_date,
                    "Asia/Shanghai",
                    [
                        (
                            phase,
                            f"{trading_date}T{opens}",
                            f"{trading_date}T{closes}",
                        )
                        for phase, opens, closes in _SESSIONS
                    ],
                )


def _active_epoch(runtime: Any) -> str:
    with runtime.read_connection() as connection:
        return str(
            connection.exec_driver_sql(
                "SELECT epoch_uid FROM market_source_epoch "
                "WHERE provider_key='NATIVE_TDX' ORDER BY started_at DESC LIMIT 1"
            ).scalar_one()
        )


def _add_szse_members(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    epoch_uid: str,
    target: datetime,
) -> tuple[tuple[int, str], ...]:
    references = ReferenceRepository(runtime, writer)
    valid_from = target - timedelta(days=60)
    for code in _SZSE_CODES:
        instrument_uid = references.create_instrument("STOCK", valid_from)
        references.add_instrument_identity_version(
            instrument_uid,
            "SZSE",
            code,
            code,
            "LISTED",
            "TRADING",
            valid_from,
        )
        references.map_instrument("NATIVE_TDX", f"0:{code}", instrument_uid, valid_from)

    with runtime.read_connection() as connection:
        sector_uid = str(
            connection.exec_driver_sql(
                "SELECT sector_uid FROM sector ORDER BY created_at LIMIT 1"
            ).scalar_one()
        )
        mapping_rows = connection.exec_driver_sql(
            "SELECT instrument_uid,external_code FROM provider_mapping "
            "WHERE provider_key='NATIVE_TDX' AND entity_kind='INSTRUMENT' "
            "AND mapping_status='RESOLVED' ORDER BY external_code"
        ).all()
    instrument_uids = [str(row.instrument_uid) for row in mapping_rows]
    members = tuple(
        (
            int(str(row.external_code).split(":", 1)[0]),
            str(row.external_code).split(":", 1)[1],
        )
        for row in mapping_rows
    )
    membership_uid = references.freeze_membership(
        sector_uid,
        _TRADING_DATE,
        instrument_uids,
        target,
        correction=True,
    )
    block_artifact = artifacts.put_bytes(
        b"offline cross-market membership fixture",
        "application/vnd.tdx.block",
    )
    artifacts.register(block_artifact)
    block_name = "block_fg.dat"
    block_version_uid = TdxStorage(runtime, writer).record_block_version(
        epoch_uid,
        TdxBlockArtifact(
            block_name,
            "fixture",
            block_artifact.size_bytes,
            None,
            block_artifact.sha256,
            target,
            "cr004-test",
        ),
        tuple(
            TdxBlockMembership("Metric Fixture Cross", 1, "ThemeMembership", market, code)
            for market, code in members
        ),
    )
    assert block_version_uid is not None
    references.record_sector_membership_source(
        membership_uid,
        "ThemeMembership",
        "Metric Fixture Cross",
        1,
        block_version_uid,
    )
    return members


def _seed_current_inputs(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    epoch_uid: str,
    target: datetime,
    members: tuple[tuple[int, str], ...],
) -> None:
    source_time = _utc_text(target)
    received_at = _utc_text(target + timedelta(seconds=10))
    IngestionService(runtime, writer, artifacts).ingest(
        epoch_uid,
        ProviderBatch(
            f"offline-current-quotes-{target.strftime('%H%M')}-{len(members)}",
            received_at,
            tuple(
                ProviderRecord(f"{market}:{code}", source_time, "10", 100, {})
                for market, code in members
            ),
        ),
    )
    with runtime.read_connection() as connection:
        quote_rows = connection.exec_driver_sql(
            "SELECT q.quote_uid,l.instrument_uid FROM market_quote q "
            "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
            "WHERE l.epoch_uid=? AND q.source_time=?",
            (epoch_uid, source_time),
        ).all()
    assert len(quote_rows) == len(members)
    server_time_raw = int(target.astimezone(_SHANGHAI).strftime("%H%M%S"))
    TdxStorage(runtime, writer).record_quote_details(
        tuple(
            (
                str(row.quote_uid),
                TdxQuoteDetail(
                    pre_close=Decimal("10"),
                    open=Decimal("10"),
                    high=Decimal("10.5"),
                    low=Decimal("9.5"),
                    amount=Decimal("1000"),
                    quote_status="VALUE",
                    server_time_raw=server_time_raw,
                ),
            )
            for row in quote_rows
        )
    )

    local_target = target.astimezone(_SHANGHAI)
    bars: list[TdxBar] = []
    for market, code in members:
        instrument = TdxInstrument(market, code, "STOCK")
        bars.extend(
            _bar(instrument, target - timedelta(minutes=offset), "1m", Decimal("100"))
            for offset in range(14, -1, -1)
        )
        for trading_date in _SAME_CLOCK_DATES:
            historical_target = datetime.fromisoformat(
                f"{trading_date}T{local_target.strftime('%H:%M:%S')}+08:00"
            ).astimezone(UTC)
            bars.extend(
                _bar(
                    instrument,
                    historical_target - timedelta(minutes=offset),
                    "1m",
                    Decimal("100"),
                )
                for offset in range(4, -1, -1)
            )
        if market == 0:
            bars.extend(
                _bar(
                    instrument,
                    target - timedelta(days=offset),
                    "1d",
                    Decimal("1000") + offset,
                )
                for offset in range(1, 21)
            )
    TdxStorage(runtime, writer).record_bars(epoch_uid, tuple(bars))


def _run_shadow_chain(
    runtime: Any, writer: Any, artifacts: Any, snapshot_uid: str
) -> dict[str, object]:
    before = _audit_surface_counts(runtime)["official_business"]
    builder = SnapshotBuilder(runtime, writer, artifacts)
    with runtime.read_connection() as connection:
        snapshot_row = connection.exec_driver_sql(
            "SELECT manifest_uid,snapshot_status FROM evaluation_snapshot WHERE snapshot_uid=?",
            (snapshot_uid,),
        ).one()
        manifest = builder.replay_manifest(str(snapshot_row.manifest_uid))
        current_bar_uids = tuple(manifest["realtime_current_bar_uids"])
        placeholders = ",".join("?" for _ in current_bar_uids)
        source_times = {
            str(row.source_time)
            for row in connection.exec_driver_sql(
                f"SELECT source_time FROM tdx_bar WHERE bar_uid IN ({placeholders})",
                current_bar_uids,
            ).all()
        }

    metric = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )
    with artifacts.open_verified(metric.evidence_sha256) as stream:
        evidence = json.load(stream)
    metric_source_times = {
        str(item["source_time"])
        for item in evidence["input_lineage"]["realtime_minute_cohort"]["targets"]
    }
    state = StateService(runtime, writer).evaluate(
        snapshot_uid,
        "AVAILABLE",
        "OBSERVING",
        [*metric.guardian_fact_uids, *metric.scout_fact_uids],
        0,
    )
    guardian = GuardianService(runtime, writer).evaluate(state.evaluation_uid)
    scout = ScoutService(runtime, writer).evaluate(guardian.guardian_uid)
    after = _audit_surface_counts(runtime)["official_business"]
    official_delta = {name: int(after[name]) - int(before[name]) for name in before}
    return {
        "snapshot_status": str(snapshot_row.snapshot_status),
        "snapshot_source_times": source_times,
        "metric_source_times": metric_source_times,
        "fitness": metric.quality.fitness_status,
        "metric_valid_members": metric.quality.valid_member_count,
        "metric_expected_members": metric.quality.tradable_expected_count,
        "guardian": guardian.guardian_effect,
        "scout": scout.scout_status,
        "scout_suppressed": scout.suppressed_by_guardian,
        "official_delta": official_delta,
    }


def _snapshot_exchange_source_times(
    runtime: Any, writer: Any, artifacts: Any, snapshot_uid: str
) -> dict[str, tuple[str, ...]]:
    builder = SnapshotBuilder(runtime, writer, artifacts)
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT manifest_uid,as_of_time FROM evaluation_snapshot WHERE snapshot_uid=?",
            (snapshot_uid,),
        ).one()
        manifest = builder.replay_manifest(str(row.manifest_uid))
        current_bar_uids = tuple(manifest["realtime_current_bar_uids"])
        placeholders = ",".join("?" for _ in current_bar_uids)
        values = connection.exec_driver_sql(
            "SELECT b.source_time,v.exchange FROM tdx_bar b "
            "JOIN instrument_identity_version v ON v.instrument_uid=b.instrument_uid "
            f"WHERE b.bar_uid IN ({placeholders}) AND v.valid_from<=? "
            "AND (v.valid_until IS NULL OR v.valid_until>?)",
            (*current_bar_uids, str(row.as_of_time), str(row.as_of_time)),
        ).all()
    grouped: dict[str, list[str]] = {"SSE": [], "SZSE": []}
    for value in values:
        grouped[str(value.exchange)].append(str(value.source_time))
    return {exchange: tuple(sorted(times)) for exchange, times in grouped.items() if times}


def _prepare_case(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    sealed_metric_snapshot: Any,
    observed_at: datetime,
    *,
    cross_market: bool = False,
) -> tuple[str, datetime, tuple[tuple[int, str], ...]]:
    sealed_metric_snapshot("HISTORICAL_REPLAY")
    _ensure_calendars(runtime, writer)
    clock = TradingClock(runtime, writer)
    target = clock.latest_completed_continuous_minute("SSE", observed_at)
    assert target is not None
    epoch_uid = _active_epoch(runtime)
    members = tuple((1, code) for code in _CODES)
    if cross_market:
        members = _add_szse_members(runtime, writer, artifacts, epoch_uid, target)
    _seed_current_inputs(runtime, writer, artifacts, epoch_uid, target, members)
    health = CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 60, 120))
    for capability in ("QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"):
        health.record(
            epoch_uid,
            capability,
            1_000_000,
            1,
            target + timedelta(seconds=10),
        )
    cohort = TdxMinuteIncrementResult(
        targets=((0, target), (1, target)),
        request_count=0,
        worker_count=0,
        expected_count=len(members),
        known_suspended_count=0,
        valid_latest_complete_count=len(members),
        missing_or_invalid_count=0,
        coverage_ppm=1_000_000,
        duration_ms=0,
        health_capability="HEALTHY",
        failure_counts={},
    )
    snapshot_uid = _prepare_metric_shadow_snapshots(
        runtime,
        writer,
        artifacts,
        epoch_uid,
        observed_at,
        f"offline-core-{target.strftime('%H%M')}-{len(members)}",
        minute_cohort=cohort,
    )[0]
    return snapshot_uid, target, members


@pytest.mark.parametrize(
    ("observed_at", "expected_target"),
    (
        (
            datetime(2026, 8, 24, 9, 45, 30, tzinfo=_SHANGHAI),
            datetime(2026, 8, 24, 1, 45, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 24, 14, 52, 38, tzinfo=_SHANGHAI),
            datetime(2026, 8, 24, 6, 52, tzinfo=UTC),
        ),
    ),
)
def test_offline_core_shadow_uses_exact_latest_right_edge(
    m3_runtime: Any,
    sealed_metric_snapshot: Any,
    observed_at: datetime,
    expected_target: datetime,
) -> None:
    runtime, writer, artifacts = m3_runtime
    snapshot_uid, target, _ = _prepare_case(
        runtime, writer, artifacts, sealed_metric_snapshot, observed_at
    )
    assert target == expected_target
    result = _run_shadow_chain(runtime, writer, artifacts, snapshot_uid)
    expected_source_time = _utc_text(expected_target)
    assert result["snapshot_status"] == "SEALED"
    assert result["snapshot_source_times"] == {expected_source_time}
    assert result["metric_source_times"] == {expected_source_time}
    assert result["fitness"] == "FIT"
    assert result["guardian"]
    assert result["scout"]
    assert result["scout_suppressed"] is True
    assert result["official_delta"]
    official_delta = result["official_delta"]
    assert isinstance(official_delta, dict)
    assert all(value == 0 for value in official_delta.values())


def test_offline_core_shadow_preserves_sse_and_szse_mapping(
    m3_runtime: Any, sealed_metric_snapshot: Any
) -> None:
    runtime, writer, artifacts = m3_runtime
    observed_at = datetime(2026, 8, 24, 9, 45, 30, tzinfo=_SHANGHAI)
    snapshot_uid, target, members = _prepare_case(
        runtime,
        writer,
        artifacts,
        sealed_metric_snapshot,
        observed_at,
        cross_market=True,
    )
    result = _run_shadow_chain(runtime, writer, artifacts, snapshot_uid)
    exchange_times = _snapshot_exchange_source_times(runtime, writer, artifacts, snapshot_uid)
    expected_source_time = _utc_text(target)
    assert Counter(market for market, _ in members) == Counter({1: 5, 0: 2})
    assert {exchange: len(times) for exchange, times in exchange_times.items()} == {
        "SSE": 5,
        "SZSE": 2,
    }
    assert all(set(times) == {expected_source_time} for times in exchange_times.values())
    assert result["snapshot_status"] == "SEALED"
    assert result["fitness"] in {"FIT", "FIT_WITH_LIMITATIONS"}
    assert result["metric_valid_members"] == 7
    assert result["metric_expected_members"] == 7
    assert result["guardian"]
    assert result["scout"]
    assert result["scout_suppressed"] is True
    official_delta = result["official_delta"]
    assert isinstance(official_delta, dict)
    assert all(value == 0 for value in official_delta.values())
