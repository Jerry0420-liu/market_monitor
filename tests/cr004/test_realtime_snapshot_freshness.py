"""CR-004 real-time snapshot observations exclude historical dependency age."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from market_monitor_analysis.metric_runner import MetricRunner
from market_monitor_analysis.snapshots import (
    PrimaryQuoteStatus,
    SnapshotBuilder,
    SnapshotFreshnessError,
)
from market_monitor_data.clock import TradingClock
from market_monitor_data.tdx.models import TdxBar, TdxInstrument
from market_monitor_data.tdx.provider import TdxMinuteIncrementResult
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import WriterQueue

from tests.cr004.test_input_manifest import AS_OF, _metric_inputs
from tests.cr004.test_metric_performance import _AS_OF, _CALENDAR_DATES

_REALTIME_LIMIT_MS = 60_000


def _record_bar(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    current_bar_uid: str,
    timestamp: datetime,
) -> str:
    with runtime.read_connection() as connection:
        epoch_uid = str(
            connection.exec_driver_sql(
                "SELECT epoch_uid FROM tdx_bar WHERE bar_uid=?", (current_bar_uid,)
            ).scalar_one()
        )
    TdxStorage(runtime, writer).record_bars(
        epoch_uid,
        (
            TdxBar(
                TdxInstrument(1, "600000", "STOCK"),
                "1m",
                timestamp,
                Decimal("10"),
                Decimal("10"),
                Decimal("10"),
                Decimal("10"),
                100,
                "SHARES",
                Decimal("1000"),
            ),
        ),
    )
    with runtime.read_connection() as connection:
        return str(
            connection.exec_driver_sql(
                "SELECT bar_uid FROM tdx_bar WHERE epoch_uid=? AND source_time=?",
                (epoch_uid, format_rfc3339(timestamp)),
            ).scalar_one()
        )


def _manifest(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    *,
    quote_uid: str,
    bar_uids: list[str],
    realtime_bar_uids: list[str],
    instrument_uid: str,
    as_of: datetime = AS_OF,
) -> tuple[SnapshotBuilder, str]:
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    return snapshots, snapshots.create_manifest(
        [quote_uid],
        as_of,
        bar_uids=bar_uids,
        primary_quote_statuses=(PrimaryQuoteStatus(instrument_uid, quote_uid, "VALID"),),
        historical_windows={"turnover_same_clock": ("2026-08-21",)},
        realtime_quote_uids=[quote_uid],
        realtime_current_bar_uids=realtime_bar_uids,
    )


def _snapshot(
    snapshots: SnapshotBuilder,
    manifest_uid: str,
    subject_uid: str,
    instrument_uid: str,
    identity_uid: str,
) -> str:
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instrument_uid, identity_uid)])
    return snapshots.create_snapshot(
        subject_uid,
        manifest_uid,
        bundle_uid,
        "SHADOW",
        ["QUOTES"],
        [],
        max_skew_ms=_REALTIME_LIMIT_MS,
        max_age_ms=_REALTIME_LIMIT_MS,
    )


def test_historical_bar_span_cannot_enlarge_realtime_snapshot_skew(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Twenty-day dependencies remain lineaged without weakening the live gate."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, current_bars, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    historical_bar = _record_bar(
        runtime, writer, current_bars[0], datetime(2026, 8, 3, 1, 34, tzinfo=UTC)
    )
    snapshots, manifest_uid = _manifest(
        runtime,
        writer,
        artifacts,
        quote_uid=quote_uid,
        bar_uids=[*current_bars, historical_bar],
        realtime_bar_uids=current_bars,
        instrument_uid=instruments[0],
    )

    snapshot_uid = _snapshot(snapshots, manifest_uid, subject_uid, instruments[0], identity_uid)

    assert snapshots.replay_manifest(manifest_uid)["schema_version"] == 4
    with runtime.read_connection() as connection:
        max_age_ms = connection.exec_driver_sql(
            "SELECT q.max_age_ms FROM evaluation_snapshot s "
            "JOIN quality_context q ON q.quality_context_uid=s.quality_context_uid "
            "WHERE s.snapshot_uid=?",
            (snapshot_uid,),
        ).scalar_one()
    assert int(max_age_ms) == _REALTIME_LIMIT_MS


@pytest.mark.parametrize("stale_current", [False, True])
def test_realtime_snapshot_rejects_stale_quote_or_current_minute(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    stale_current: bool,
) -> None:
    """Old live observations cannot pass merely because historical input exists."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, current_bars, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    stale_bar = _record_bar(
        runtime, writer, current_bars[0], datetime(2026, 8, 24, 1, 33, tzinfo=UTC)
    )
    historical_bar = _record_bar(
        runtime, writer, current_bars[0], datetime(2026, 8, 3, 1, 34, tzinfo=UTC)
    )
    as_of = AS_OF if stale_current else AS_OF + timedelta(seconds=1)
    snapshots, manifest_uid = _manifest(
        runtime,
        writer,
        artifacts,
        quote_uid=quote_uid,
        bar_uids=[*current_bars, stale_bar, historical_bar],
        realtime_bar_uids=[stale_bar] if stale_current else current_bars,
        instrument_uid=instruments[0],
        as_of=as_of,
    )

    with pytest.raises(SnapshotFreshnessError, match="realtime source age"):
        _snapshot(snapshots, manifest_uid, subject_uid, instruments[0], identity_uid)


def test_realtime_manifest_cannot_omit_live_quote_from_its_gate(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail closed if a caller tries to make a stale quote invisible to v4."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, current_bars, instruments, _, _ = _metric_inputs(m3_runtime)

    with pytest.raises(ValueError, match="realtime quote"):
        SnapshotBuilder(runtime, writer, artifacts).create_manifest(
            [quote_uid],
            AS_OF,
            bar_uids=current_bars,
            primary_quote_statuses=(PrimaryQuoteStatus(instruments[0], quote_uid, "VALID"),),
            historical_windows={"turnover_same_clock": ("2026-08-21",)},
            realtime_quote_uids=[],
            realtime_current_bar_uids=current_bars,
        )


def test_realtime_current_bar_right_edge_is_complete_at_snapshot_time(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """A right-edge bar stamped at as-of is complete at snapshot time."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, current_bars, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    completed_bar = _record_bar(runtime, writer, current_bars[0], AS_OF)
    snapshots, manifest_uid = _manifest(
        runtime,
        writer,
        artifacts,
        quote_uid=quote_uid,
        bar_uids=[*current_bars, completed_bar],
        realtime_bar_uids=[completed_bar],
        instrument_uid=instruments[0],
    )
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instruments[0], identity_uid)])

    snapshot_uid = snapshots.create_snapshot(
        subject_uid,
        manifest_uid,
        bundle_uid,
        "SHADOW",
        ["QUOTES"],
        [],
        max_skew_ms=180_000,
        max_age_ms=_REALTIME_LIMIT_MS,
    )

    assert snapshot_uid


def test_shadow_preparation_pins_the_exact_realtime_minute_cohort(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Any,
) -> None:
    """Live preparation must retain v5 exact-minute lineage through metric execution."""
    from scripts.tdx_runner import _prepare_metric_shadow_snapshots

    runtime, writer, artifacts = m3_runtime
    sealed_metric_snapshot("HISTORICAL_REPLAY")
    calendar = TradingClock(runtime, writer)
    for exchange in ("SSE", "SZSE"):
        for trading_date in _CALENDAR_DATES:
            calendar.import_day(
                exchange,
                trading_date,
                "Asia/Shanghai",
                [
                    ("CONTINUOUS_AM", f"{trading_date}T01:30:00Z", f"{trading_date}T03:30:00Z"),
                    ("CONTINUOUS_PM", f"{trading_date}T05:00:00Z", f"{trading_date}T07:00:00Z"),
                ],
            )
    with runtime.read_connection() as connection:
        epoch_uid = str(
            connection.exec_driver_sql(
                "SELECT epoch_uid FROM market_source_epoch "
                "WHERE provider_key='NATIVE_TDX' ORDER BY started_at DESC LIMIT 1"
            ).scalar_one()
        )

    target = _AS_OF - timedelta(minutes=1)
    minute_cohort = TdxMinuteIncrementResult(
        ((0, target), (1, target)),
        5,
        4,
        5,
        0,
        5,
        0,
        1_000_000,
        1,
        "HEALTHY",
        {},
    )
    shadow_round_uid = "shadow-round-fixture-001"
    snapshot_uids = _prepare_metric_shadow_snapshots(
        runtime,
        writer,
        artifacts,
        epoch_uid,
        _AS_OF,
        shadow_round_uid,
        minute_cohort=minute_cohort,
    )
    next_snapshot_uids = _prepare_metric_shadow_snapshots(
        runtime,
        writer,
        artifacts,
        epoch_uid,
        _AS_OF,
        "shadow-round-fixture-002",
        minute_cohort=minute_cohort,
    )
    assert next_snapshot_uids[0] != snapshot_uids[0]

    with runtime.read_connection() as connection:
        manifest_uid = str(
            connection.exec_driver_sql(
                "SELECT manifest_uid FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uids[0],),
            ).scalar_one()
        )
        max_skew_ms = int(
            connection.exec_driver_sql(
                "SELECT max_skew_ms FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uids[0],),
            ).scalar_one()
        )
    document = SnapshotBuilder(runtime, writer, artifacts).replay_manifest(manifest_uid)
    assert document["schema_version"] == 5
    assert document["shadow_round_uid"] == shadow_round_uid
    assert document["realtime_quote_uids"] == document["quote_uids"]
    assert document["realtime_current_bar_uids"]
    assert max_skew_ms == _REALTIME_LIMIT_MS
    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uids[0],
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )
    with artifacts.open_verified(result.evidence_sha256) as stream:
        evidence = json.load(stream)
    assert evidence["manifest_schema_version"] == 5
    assert evidence["input_lineage"]["shadow_round_uid"] == shadow_round_uid
