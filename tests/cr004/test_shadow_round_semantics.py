"""CR-004 acceptance Shadow rounds require fresh, distinct live lineage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest
from market_monitor_data.tdx.provider import NativeTdxProvider
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime


def _calendar(path: Path) -> None:
    from tests.cr002.test_tdx_runner import _owner_calendar_document

    path.write_text(json.dumps(_owner_calendar_document()), encoding="utf-8")


def _fit_warmup(monkeypatch: pytest.MonkeyPatch, runner: object) -> None:
    class _Warmup:
        def __init__(self, *_: object) -> None:
            pass

        def assess(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(
                state="FIT", minute_complete=1, daily_complete=1, coverage_ppm=1_000_000
            )

    monkeypatch.setattr(runner, "TdxHistoricalLoader", _Warmup)
    monkeypatch.setattr(runner, "_bootstrap_current_live_tail", lambda *_: None)


def test_metric_shadow_sweeps_are_distinct_fresh_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break if two requested acceptance rounds reuse one sealed snapshot lineage."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    prepared: list[object] = []
    received: list[tuple[str, ...]] = []
    snapshots = iter((("fresh-shadow-1",), ("fresh-shadow-2",)))

    def prepare(*args: object, **_kwargs: object) -> tuple[str, ...]:
        prepared.append(args[-1])
        return next(snapshots)

    monkeypatch.setattr(runner, "_prepare_metric_shadow_snapshots", prepare)

    def metric_shadow_results(*args: Any) -> list[object]:
        received.append(tuple(args[-1]))
        return []

    monkeypatch.setattr(
        runner,
        "_metric_shadow_results",
        metric_shadow_results,
    )
    monkeypatch.setattr(runner, "_shadow_calibration_report", lambda *_: {"state": "SHADOW"})
    polls: list[datetime] = []

    def poll_once(_: object, _epoch_uid: str, observed_at: datetime) -> SimpleNamespace:
        polls.append(observed_at)
        return SimpleNamespace(
            requested=1,
            returned=1,
            quarantined=0,
            duration_ms=1,
            health_capabilities={},
        )

    monkeypatch.setattr(NativeTdxProvider, "poll_once", poll_once)

    def refresh(
        _: object,
        _epoch_uid: str,
        targets: dict[int, datetime],
        _observed_at: datetime,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
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
        )

    monkeypatch.setattr(NativeTdxProvider, "refresh_primary_minute_bars", refresh)
    current = datetime(2026, 8, 21, 1, 31, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        value = current
        current += timedelta(seconds=1)
        return value

    report = runner.run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=2,
        metric_shadow=True,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        trading_calendar_file=calendar,
        now=now,
        node_health=lambda: (),
    )

    assert len(prepared) == 2
    assert len(set(prepared)) == 2
    assert len(polls) == 2
    assert received == [("fresh-shadow-1",), ("fresh-shadow-2",)]
    assert report["shadow_snapshots"] == ["fresh-shadow-1", "fresh-shadow-2"]
    assert report["metric_repeatability"]["mode"] == "FRESH_ROUND"
    assert len({round_["round_uid"] for round_ in report["shadow_rounds"]}) == 2
    assert [round_["snapshot_uids"] for round_ in report["shadow_rounds"]] == [
        ["fresh-shadow-1"],
        ["fresh-shadow-2"],
    ]
    assert all(
        {"acquisition_elapsed_ms", "preparation_elapsed_ms", "metric_elapsed_ms", "elapsed_ms"}
        <= round_.keys()
        for round_ in report["shadow_rounds"]
    )


def test_metric_shadow_quote_and_minute_acquisitions_overlap_on_one_cycle_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break if Quote and exact-minute acquisition become serial or use different cycle time."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        lambda *_args, **_kwargs: ("overlap-shadow",),
    )
    monkeypatch.setattr(runner, "_metric_shadow_results", lambda *_: [])
    monkeypatch.setattr(runner, "_shadow_calibration_report", lambda *_: {"state": "SHADOW"})

    rendezvous = Barrier(2)
    observed: list[tuple[str, datetime, tuple[tuple[int, datetime], ...] | None]] = []

    def poll_once(_: object, _epoch_uid: str, observed_at: datetime) -> SimpleNamespace:
        observed.append(("quote", observed_at, None))
        rendezvous.wait(timeout=1)
        return SimpleNamespace(
            requested=1,
            returned=1,
            quarantined=0,
            duration_ms=1,
            health_capabilities={},
        )

    def refresh(
        _: object,
        _epoch_uid: str,
        targets: dict[int, datetime],
        observed_at: datetime,
        **_kwargs: object,
    ) -> SimpleNamespace:
        frozen_targets = tuple(sorted(targets.items()))
        observed.append(("minute", observed_at, frozen_targets))
        rendezvous.wait(timeout=1)
        return SimpleNamespace(
            targets=frozen_targets,
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
        )

    monkeypatch.setattr(NativeTdxProvider, "poll_once", poll_once)
    monkeypatch.setattr(NativeTdxProvider, "refresh_primary_minute_bars", refresh)
    cycle_time = datetime(2026, 8, 21, 1, 31, 10, tzinfo=UTC)

    report = runner.run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=1,
        metric_shadow=True,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        trading_calendar_file=calendar,
        now=lambda: cycle_time,
        node_health=lambda: (),
    )

    assert {item[0] for item in observed} == {"quote", "minute"}
    assert {item[1] for item in observed} == {cycle_time}
    assert next(item[2] for item in observed if item[0] == "minute") == (
        (0, datetime(2026, 8, 21, 1, 31, tzinfo=UTC)),
        (1, datetime(2026, 8, 21, 1, 31, tzinfo=UTC)),
    )
    round_ = report["shadow_rounds"][0]
    assert round_["market_source_epoch"] == report["epoch_uid"]
    assert len(round_["primary_universe_version"]) == 64
    assert report["timing"]["stages"]["quote_acquisition"]["durations_ms"] == [1]
    assert report["timing"]["stages"]["minute_cohort_acquisition"]["durations_ms"] == [1]


def test_metric_shadow_allows_limited_minute_cohort_and_keeps_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5,195/5,210 exact cohort remains a limited Shadow input, not synthetic FIT."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    captured: list[object] = []

    def prepare_limited(*_args: object, **kwargs: object) -> tuple[str, ...]:
        captured.append(kwargs["minute_cohort"])
        return ("limited-shadow",)

    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        prepare_limited,
    )
    monkeypatch.setattr(runner, "_metric_shadow_results", lambda *_: [])
    monkeypatch.setattr(runner, "_shadow_calibration_report", lambda *_: {"state": "SHADOW"})
    monkeypatch.setattr(
        NativeTdxProvider,
        "poll_once",
        lambda *_: SimpleNamespace(
            requested=5_210, returned=5_203, quarantined=0, duration_ms=1, health_capabilities={}
        ),
    )
    monkeypatch.setattr(
        NativeTdxProvider,
        "refresh_primary_minute_bars",
        lambda _self, _epoch_uid, targets, _observed_at, **_kwargs: SimpleNamespace(
            targets=tuple(sorted(targets.items())),
            request_count=5_210,
            worker_count=4,
            expected_count=5_210,
            known_suspended_count=0,
            valid_latest_complete_count=5_195,
            missing_or_invalid_count=15,
            coverage_ppm=997_120,
            duration_ms=1,
            health_capability="DEGRADED",
            failure_counts={"NON_TARGET": 15},
        ),
    )

    report = runner.run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=1,
        metric_shadow=True,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        trading_calendar_file=calendar,
        now=lambda: datetime(2026, 8, 21, 1, 31, 10, tzinfo=UTC),
        node_health=lambda: (),
    )

    assert len(captured) == 1
    cohort = report["shadow_rounds"][0]["minute_cohort"]
    assert cohort["coverage_ppm"] == 997_120
    assert cohort["missing_or_invalid_count"] == 15
    assert cohort["failure_counts"] == {"NON_TARGET": 15}
    assert cohort["valid_latest_complete_count"] == 5_195


@pytest.mark.parametrize(
    ("failing_task", "exception_type", "message"),
    (
        ("quote", TimeoutError, "quote timeout"),
        ("minute", RuntimeError, "minute acquisition failed"),
        ("minute_wrong_target", ValueError, "minute cohort target mismatch"),
        ("minute_missing", ValueError, "minute cohort incomplete"),
    ),
)
def test_metric_shadow_bad_parallel_acquisition_creates_no_snapshot_or_official_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_task: str,
    exception_type: type[Exception],
    message: str,
) -> None:
    """Break if either concurrent acquisition can fail after snapshot or OFFICIAL mutation."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    prepared: list[bool] = []
    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        lambda *_args, **_kwargs: prepared.append(True),
    )
    rendezvous = Barrier(2)

    def poll_once(_: object, _epoch_uid: str, _observed_at: datetime) -> SimpleNamespace:
        rendezvous.wait(timeout=1)
        if failing_task == "quote":
            raise exception_type(message)
        return SimpleNamespace(
            requested=1,
            returned=1,
            quarantined=0,
            duration_ms=1,
            health_capabilities={},
        )

    def refresh(
        _: object,
        _epoch_uid: str,
        targets: dict[int, datetime],
        _observed_at: datetime,
        **_kwargs: object,
    ) -> SimpleNamespace:
        rendezvous.wait(timeout=1)
        if failing_task == "minute":
            raise exception_type(message)
        minute_missing = failing_task == "minute_missing"
        returned_targets = tuple(sorted(targets.items()))
        if failing_task == "minute_wrong_target":
            returned_targets = tuple(
                (market, target + timedelta(minutes=1)) for market, target in returned_targets
            )
        return SimpleNamespace(
            targets=returned_targets,
            request_count=1,
            worker_count=1,
            expected_count=1,
            known_suspended_count=0,
            valid_latest_complete_count=0 if minute_missing else 1,
            missing_or_invalid_count=1 if minute_missing else 0,
            coverage_ppm=0 if minute_missing else 1_000_000,
            duration_ms=1,
            health_capability="UNHEALTHY" if minute_missing else "HEALTHY",
            failure_counts={},
        )

    monkeypatch.setattr(NativeTdxProvider, "poll_once", poll_once)
    monkeypatch.setattr(NativeTdxProvider, "refresh_primary_minute_bars", refresh)

    with pytest.raises(exception_type, match=message):
        runner.run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            metric_shadow=True,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
            trading_calendar_file=calendar,
            now=lambda: datetime(2026, 8, 21, 1, 31, 10, tzinfo=UTC),
            node_health=lambda: (),
        )

    assert prepared == []
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    try:
        assert all(
            count == 0
            for count in runner._audit_surface_counts(runtime)["official_business"].values()
        )
    finally:
        runtime.close()


def test_metric_shadow_refreshes_primary_minute_cohort_for_each_fixed_target_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each cycle reacquires the same pinned legal minute instead of reusing its cohort."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        lambda *_args, **_kwargs: ("cohort-shadow",),
    )
    monkeypatch.setattr(runner, "_metric_shadow_results", lambda *_: [])
    monkeypatch.setattr(runner, "_shadow_calibration_report", lambda *_: {"state": "SHADOW"})
    monkeypatch.setattr(
        NativeTdxProvider,
        "poll_once",
        lambda *_: SimpleNamespace(
            requested=1,
            returned=1,
            quarantined=0,
            duration_ms=1,
            health_capabilities={},
        ),
    )
    refreshes: list[tuple[tuple[int, datetime], ...]] = []

    def refresh(
        _: object,
        _epoch_uid: str,
        targets: dict[int, datetime],
        _observed_at: datetime,
        **_kwargs: object,
    ) -> SimpleNamespace:
        refreshes.append(tuple(sorted(targets.items())))
        return SimpleNamespace(
            targets=tuple(sorted(targets.items())),
            request_count=5_216,
            worker_count=4,
            expected_count=5_216,
            known_suspended_count=0,
            valid_latest_complete_count=5_216,
            missing_or_invalid_count=0,
            coverage_ppm=1_000_000,
            duration_ms=1,
            health_capability="HEALTHY",
            failure_counts={},
        )

    monkeypatch.setattr(NativeTdxProvider, "refresh_primary_minute_bars", refresh)
    report = runner.run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=2,
        metric_shadow=True,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        trading_calendar_file=calendar,
        now=lambda: datetime(2026, 8, 21, 1, 31, 10, tzinfo=UTC),
        node_health=lambda: (),
    )

    assert refreshes == [
        (
            (0, datetime(2026, 8, 21, 1, 31, tzinfo=UTC)),
            (1, datetime(2026, 8, 21, 1, 31, tzinfo=UTC)),
        ),
        (
            (0, datetime(2026, 8, 21, 1, 31, tzinfo=UTC)),
            (1, datetime(2026, 8, 21, 1, 31, tzinfo=UTC)),
        ),
    ]
    assert [round_["minute_cohort"]["targets"] for round_ in report["shadow_rounds"]] == [
        [
            {"market": 0, "source_time": "2026-08-21T01:31:00.000000Z"},
            {"market": 1, "source_time": "2026-08-21T01:31:00.000000Z"},
        ],
        [
            {"market": 0, "source_time": "2026-08-21T01:31:00.000000Z"},
            {"market": 1, "source_time": "2026-08-21T01:31:00.000000Z"},
        ],
    ]


def test_metric_shadow_rejects_non_continuous_market_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lunch/closed runs cannot create an acceptance Shadow round."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)

    with pytest.raises(ValueError, match="continuous"):
        runner.run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            metric_shadow=True,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
            trading_calendar_file=calendar,
            now=lambda: datetime(2026, 8, 21, 4, tzinfo=UTC),
            node_health=lambda: (),
        )


def test_repeatability_iterations_cannot_be_counted_as_multiple_acceptance_rounds(
    tmp_path: Path,
) -> None:
    """Repeatability remains a one-round determinism check, never a two-round substitute."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)

    with pytest.raises(ValueError, match="repeatability"):
        runner.run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=2,
            metric_shadow=True,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
            metric_iterations=2,
            trading_calendar_file=calendar,
            now=lambda: datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
            node_health=lambda: (),
        )


def test_repeatability_cli_name_is_explicit() -> None:
    """The user-facing flag must not describe repeat evaluation as acceptance rounds."""
    from scripts.tdx_runner import _parser

    arguments = _parser().parse_args(
        ["--data-dir", "runtime/shadow", "--repeatability-iterations", "2"]
    )

    assert arguments.metric_iterations == 2


def test_metric_shadow_refuses_to_seal_a_cohort_overtaken_by_a_new_legal_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5,216-symbol increment must finish before its next legal minute is due."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    monkeypatch.setattr(
        NativeTdxProvider,
        "poll_once",
        lambda *_: SimpleNamespace(
            requested=1,
            returned=1,
            quarantined=0,
            duration_ms=1,
            health_capabilities={},
        ),
    )
    virtual_now = datetime(2026, 8, 21, 1, 31, tzinfo=UTC)

    def refresh(
        _: object,
        _epoch_uid: str,
        targets: dict[int, datetime],
        _observed_at: datetime,
        **_kwargs: object,
    ) -> SimpleNamespace:
        nonlocal virtual_now
        virtual_now = datetime(2026, 8, 21, 1, 32, tzinfo=UTC)
        return SimpleNamespace(
            targets=tuple(sorted(targets.items())),
            request_count=5_216,
            worker_count=4,
            expected_count=5_216,
            known_suspended_count=0,
            valid_latest_complete_count=5_216,
            missing_or_invalid_count=0,
            coverage_ppm=1_000_000,
            duration_ms=42_000,
            health_capability="HEALTHY",
            failure_counts={},
        )

    monkeypatch.setattr(NativeTdxProvider, "refresh_primary_minute_bars", refresh)
    monkeypatch.setattr(
        runner,
        "_prepare_metric_shadow_snapshots",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale minute cohort reached snapshot preparation")
        ),
    )

    with pytest.raises(ValueError, match="minute cohort became stale"):
        runner.run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            metric_shadow=True,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
            trading_calendar_file=calendar,
            now=lambda: virtual_now,
            node_health=lambda: (),
        )


def test_metric_shadow_uses_post_increment_time_as_the_snapshot_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1m sweep may not backdate a snapshot to before its bar collection completed."""
    import scripts.tdx_runner as runner
    from tests.cr002.test_tdx_runner import _Gateway

    calendar = tmp_path / "calendar.json"
    _calendar(calendar)
    _fit_warmup(monkeypatch, runner)
    monkeypatch.setattr(
        NativeTdxProvider,
        "poll_once",
        lambda *_: SimpleNamespace(
            requested=1,
            returned=1,
            quarantined=0,
            duration_ms=1,
            health_capabilities={},
        ),
    )
    monkeypatch.setattr(
        NativeTdxProvider,
        "refresh_primary_minute_bars",
        lambda _, _epoch_uid, targets, _observed_at, **_kwargs: SimpleNamespace(
            targets=tuple(sorted(targets.items())),
            request_count=5_216,
            worker_count=4,
            expected_count=5_216,
            known_suspended_count=0,
            valid_latest_complete_count=5_216,
            missing_or_invalid_count=0,
            coverage_ppm=1_000_000,
            duration_ms=42_000,
            health_capability="HEALTHY",
            failure_counts={},
        ),
    )
    prepared_at: list[datetime] = []

    def prepare_snapshots(
        _runtime: object,
        _writer: object,
        _artifacts: object,
        _epoch: object,
        observed_at: datetime,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[str, ...]:
        prepared_at.append(observed_at)
        return ("fresh-shadow",)

    monkeypatch.setattr(runner, "_prepare_metric_shadow_snapshots", prepare_snapshots)
    monkeypatch.setattr(runner, "_metric_shadow_results", lambda *_: [])
    monkeypatch.setattr(runner, "_shadow_calibration_report", lambda *_: {"state": "SHADOW"})
    times = iter(
        (
            datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
            datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
            datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
            datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
            datetime(2026, 8, 21, 1, 31, 42, tzinfo=UTC),
            datetime(2026, 8, 21, 1, 31, 42, tzinfo=UTC),
            datetime(2026, 8, 21, 1, 31, 42, tzinfo=UTC),
        )
    )

    runner.run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=1,
        metric_shadow=True,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        trading_calendar_file=calendar,
        now=lambda: next(times),
        node_health=lambda: (),
    )

    assert prepared_at == [datetime(2026, 8, 21, 1, 31, 42, tzinfo=UTC)]
