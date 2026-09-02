"""CR-004 full-metric timing output must use the existing TDX percentile policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from market_monitor_data.clock import TradingClock
from market_monitor_data.tdx.provider import (
    NativeTdxProvider,
    TdxMinuteIncrementResult,
    TdxUniverse,
)
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

from scripts.tdx_runner import (
    _metric_shadow_results,
    _parser,
    _prepare_metric_shadow_snapshots,
    _run_metric_shadow,
    _shadow_calibration_report,
    main,
    metric_timing_summary,
)

_AS_OF = datetime(2026, 8, 24, 1, 45, tzinfo=UTC)
_CALENDAR_DATES = (
    "2026-07-27",
    "2026-07-28",
    "2026-07-29",
    "2026-07-30",
    "2026-07-31",
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-17",
    "2026-08-18",
    "2026-08-19",
    "2026-08-20",
    "2026-08-21",
    "2026-08-24",
)


def test_metric_timing_summary_uses_existing_nearest_rank_percentiles() -> None:
    """Break if metric timing silently uses a different P50/P95 policy than TDX sweeps."""
    assert metric_timing_summary((7, 11, 13, 17, 23)) == {
        "count": 5,
        "durations_ms": [7, 11, 13, 17, 23],
        "p50_ms": 13,
        "p95_ms": 23,
        "max_ms": 23,
    }


def test_metric_shadow_cli_requires_explicit_threshold_versions_and_iterations() -> None:
    """Break if the live Shadow entry can silently select a threshold default."""
    parser = _parser()
    arguments = parser.parse_args(
        [
            "--data-dir",
            "runtime/cr004-shadow",
            "--metric-shadow",
            "--guardian-threshold-version",
            "guardian-thresholds-v1.0-prod",
            "--scout-threshold-version",
            "scout-thresholds-v1.0-prod",
            "--metric-iterations",
            "20",
        ]
    )

    assert arguments.metric_shadow is True
    assert arguments.metric_iterations == 20
    with pytest.raises(SystemExit):
        main(["--data-dir", "runtime/cr004-shadow", "--metric-shadow"])


def test_metric_shadow_preparation_seals_new_shadow_inputs_only(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if live CR-004 preparation reuses replay or touches OFFICIAL state."""
    runtime, writer, artifacts = m3_runtime
    replay_snapshot_uid = sealed_metric_snapshot("HISTORICAL_REPLAY")
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
                "SELECT epoch_uid FROM market_source_epoch WHERE provider_key='NATIVE_TDX'"
            ).scalar_one()
        )
        before_official = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM evaluation_snapshot WHERE evaluation_disposition='OFFICIAL'"
            ).scalar_one()
        )

    snapshot_uids = _prepare_metric_shadow_snapshots(runtime, writer, artifacts, epoch_uid, _AS_OF)

    assert len(snapshot_uids) == 1
    assert replay_snapshot_uid not in snapshot_uids
    with runtime.read_connection() as connection:
        snapshots = connection.exec_driver_sql(
            "SELECT snapshot_uid,evaluation_disposition,snapshot_status "
            "FROM evaluation_snapshot ORDER BY snapshot_uid"
        ).all()
        after_official = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM evaluation_snapshot WHERE evaluation_disposition='OFFICIAL'"
            ).scalar_one()
        )
    assert {
        (str(row.snapshot_uid), str(row.evaluation_disposition), str(row.snapshot_status))
        for row in snapshots
    } == {
        (replay_snapshot_uid, "HISTORICAL_REPLAY", "SEALED"),
        (snapshot_uids[0], "SHADOW", "SEALED"),
    }
    assert after_official == before_official


def test_metric_shadow_preparation_seals_exact_primary_minute_cohort(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """A current 1m tail is eligible only when it matches the legal cohort target."""
    from market_monitor_analysis.snapshots import SnapshotBuilder

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
                "SELECT epoch_uid FROM market_source_epoch WHERE provider_key='NATIVE_TDX'"
            ).scalar_one()
        )
    cohort = TdxMinuteIncrementResult(
        (
            (0, datetime(2026, 8, 24, 1, 44, tzinfo=UTC)),
            (1, datetime(2026, 8, 24, 1, 44, tzinfo=UTC)),
        ),
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

    snapshot_uids = _prepare_metric_shadow_snapshots(
        runtime,
        writer,
        artifacts,
        epoch_uid,
        _AS_OF,
        "exact-cohort-shadow",
        minute_cohort=cohort,
    )

    with runtime.read_connection() as connection:
        manifest_uid = str(
            connection.exec_driver_sql(
                "SELECT manifest_uid FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uids[0],),
            ).scalar_one()
        )
    document = SnapshotBuilder(runtime, writer, artifacts).replay_manifest(manifest_uid)
    assert document["schema_version"] == 5
    assert document["realtime_minute_cohort"]["valid_latest_complete_count"] == 5
    assert document["realtime_minute_cohort"]["targets"] == [
        {"market": 0, "source_time": "2026-08-24T01:44:00.000000Z"},
        {"market": 1, "source_time": "2026-08-24T01:44:00.000000Z"},
    ]


def test_metric_shadow_runner_warms_then_evaluates_only_fresh_shadow_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break if the live CR-004 entry skips warm-up or consumes stale Shadow snapshots."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import (
        _Gateway,
        _listing_reference_file,
        _owner_calendar_document,
    )

    calendar = tmp_path / "calendar.json"
    calendar.write_text(__import__("json").dumps(_owner_calendar_document()), encoding="utf-8")
    listing_reference = _listing_reference_file(tmp_path)
    warm_calls: list[tuple[str, int]] = []
    received_snapshots: list[tuple[str, ...]] = []

    class _Warmup:
        def __init__(self, *_: object) -> None:
            pass

        def assess(self, epoch_uid: str, universe: TdxUniverse, _: datetime) -> SimpleNamespace:
            warm_calls.append((epoch_uid, len(universe.primary)))
            return SimpleNamespace(
                state="FIT", minute_complete=1, daily_complete=1, coverage_ppm=1_000_000
            )

    monkeypatch.setattr(runner, "TdxHistoricalLoader", _Warmup)
    monkeypatch.setattr(runner, "_bootstrap_current_live_tail", lambda *_: None)
    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        lambda *_args, **_kwargs: ("fresh-shadow",),
    )

    def metric_shadow_results(*args: Any) -> list[object]:
        received_snapshots.append(tuple(args[-1]))
        return []

    monkeypatch.setattr(
        runner,
        "_metric_shadow_results",
        metric_shadow_results,
    )
    monkeypatch.setattr(runner, "_shadow_calibration_report", lambda *_: {"state": "SHADOW"})
    monkeypatch.setattr(
        NativeTdxProvider,
        "refresh_primary_minute_bars",
        lambda _self, _epoch_uid, targets, _observed_at, **_kwargs: SimpleNamespace(
            targets=tuple(sorted(targets.items())),
            request_count=1,
            worker_count=1,
            expected_count=1,
            known_suspended_count=0,
            valid_latest_complete_count=1,
            missing_or_invalid_count=0,
            coverage_ppm=1_000_000,
            duration_ms=1,
            health_capability="HEALTHY",
            failure_counts={},
        ),
    )
    current = datetime(2026, 8, 21, 1, 31, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        result = current
        current = current.replace(second=current.second + 1)
        return result

    report = runner.run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=1,
        metric_shadow=True,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        trading_calendar_file=calendar,
        listing_reference_file=listing_reference,
        now=now,
        node_health=lambda: (),
    )

    assert warm_calls and warm_calls[0][1] == 1
    assert received_snapshots == [("fresh-shadow",)]
    assert report["warmup"]["state"] == "FIT"
    assert report["shadow_snapshots"] == ["fresh-shadow"]
    assert report["watermarks"] == {}
    assert report["side_effect_audit"]["passed"] is True
    assert report["timing"]["fresh_round"]["count"] == 1


def test_metric_shadow_runs_each_existing_sealed_snapshot_for_requested_iterations(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if CLI metric mode skips a sealed Shadow evaluation or timing record."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW")

    records = _run_metric_shadow(
        runtime,
        writer,
        artifacts,
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        2,
    )

    assert [record["snapshot_uid"] for record in records] == [snapshot_uid, snapshot_uid]
    assert all(record["fitness_status"] == "FIT" for record in records)
    assert metric_timing_summary([int(record["elapsed_ms"]) for record in records])["count"] == 2


def test_metric_shadow_emits_append_only_calibration_evidence(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if the CLI's metric-shadow result omits its CR-005 calibration record."""
    runtime, writer, artifacts = m3_runtime
    sealed_metric_snapshot("SHADOW")
    results = _metric_shadow_results(
        runtime,
        writer,
        artifacts,
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        2,
    )

    report = _shadow_calibration_report(
        runtime,
        writer,
        artifacts,
        results,
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
    )

    assert report["validation_kind"] == "SHADOW"
    assert report["metric_run_count"] == 2
    assert len(str(report["evidence_sha256"])) == 64
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_validation").scalar() == 2
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_activation").scalar() == 0


def test_metric_shadow_rejects_unfit_historical_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break if metric Shadow proceeds before its historical inputs are FIT."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway, _owner_calendar_document

    calendar = tmp_path / "calendar.json"
    calendar.write_text(__import__("json").dumps(_owner_calendar_document()), encoding="utf-8")

    class _Warmup:
        def __init__(self, *_: object) -> None:
            pass

        def assess(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(
                state="WARMING_UP", minute_complete=0, daily_complete=0, coverage_ppm=0
            )

    monkeypatch.setattr(runner, "TdxHistoricalLoader", _Warmup)
    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        lambda *_: pytest.fail("unfit warm-up must not create Shadow snapshots"),
    )

    with pytest.raises(ValueError, match="requires FIT historical warm-up"):
        runner.run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            metric_shadow=True,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
            trading_calendar_file=calendar,
            now=lambda: datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
        )
