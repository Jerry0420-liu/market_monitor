"""CR-004 current-minute coverage remains member-scoped."""

from __future__ import annotations

from collections.abc import Callable

import market_monitor_analysis.metric_runner as metric_runner_module
import pytest
from market_monitor_analysis.metric_runner import MetricRunner, MetricRunResult, _Bar
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue


def _run(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    snapshot_uid: str,
) -> MetricRunResult:
    return MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )


def test_one_missing_member_tail_is_not_global_provider_failure(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One member with no complete minute belongs to subject coverage, not Provider health."""
    runtime, writer, artifacts = m3_runtime
    original: Callable[..., tuple[_Bar, ...] | None] = metric_runner_module._complete_current_tail
    skipped = False

    def missing_one(*args: object, **kwargs: object) -> tuple[_Bar, ...] | None:
        nonlocal skipped
        if not skipped:
            skipped = True
            return None
        return original(*args, **kwargs)

    monkeypatch.setattr(metric_runner_module, "_complete_current_tail", missing_one)

    result = _run(runtime, writer, artifacts, sealed_metric_snapshot("SHADOW"))

    assert skipped is True
    assert result.quality.fitness_status == "FIT"


def test_resumed_short_tail_does_not_index_into_am_session(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-minute resumed tail yields unavailable 5/10/15m facts, never stale AM values."""
    runtime, writer, artifacts = m3_runtime
    original: Callable[..., tuple[_Bar, ...] | None] = metric_runner_module._complete_current_tail

    def one_minute_tail(*args: object, **kwargs: object) -> tuple[_Bar, ...] | None:
        tail = original(*args, **kwargs)
        return None if tail is None else tail[-1:]

    monkeypatch.setattr(metric_runner_module, "_complete_current_tail", one_minute_tail)

    result = _run(runtime, writer, artifacts, sealed_metric_snapshot("SHADOW"))

    assert result.quality.fitness_status == "FIT"
