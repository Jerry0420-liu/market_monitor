"""CR-004 sealed Shadow/Replay metric-runner behavior."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from market_monitor_analysis.canonical import canonical_hash
from market_monitor_analysis.market_metrics import MarketMetricProducer
from market_monitor_analysis.metric_runner import (
    MetricInputLineageError,
    MetricRunInProgressError,
    MetricRunner,
    MetricRunnerDispositionError,
)
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue


def test_shadow_runner_persists_value_metric_facts_with_explicit_threshold_lineage(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if a Shadow metric run skips canonical facts or threshold lineage."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW")

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    assert result.snapshot_uid == snapshot_uid
    assert result.producer_version == MarketMetricProducer.RULE_VERSION
    assert result.guardian_fact_uids
    assert result.scout_fact_uids
    assert result.quality.fitness_status == "FIT"
    with runtime.read_connection() as connection:
        lineages = connection.exec_driver_sql(
            "SELECT r.rule_key,t.version FROM rule_execution r "
            "JOIN threshold_version t ON t.threshold_version_uid=r.threshold_version_uid "
            "WHERE r.snapshot_uid=? ORDER BY r.rule_key",
            (snapshot_uid,),
        ).all()
    assert [(str(row.rule_key), str(row.version)) for row in lineages] == [
        ("CR004_GUARDIAN_METRICS", "guardian-thresholds-v1.0-prod"),
        ("CR004_SCOUT_METRICS", "scout-thresholds-v1.0-prod"),
    ]


def test_shadow_metric_evidence_binds_the_sealed_input_lineage(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW")

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    with artifacts.open_verified(result.evidence_sha256) as stream:
        evidence = json.load(stream)
    with runtime.read_connection() as connection:
        snapshot = connection.exec_driver_sql(
            "SELECT s.subject_uid,s.canonical_hash,m.manifest_uid,m.artifact_sha256 "
            "FROM evaluation_snapshot s JOIN input_manifest m ON m.manifest_uid=s.manifest_uid "
            "WHERE s.snapshot_uid=?",
            (snapshot_uid,),
        ).one()
    assert evidence["schema_version"] == 2
    assert evidence["snapshot_uid"] == snapshot_uid
    assert evidence["subject_uid"] == str(snapshot.subject_uid)
    assert evidence["manifest_uid"] == str(snapshot.manifest_uid)
    assert evidence["manifest_artifact_sha256"] == str(snapshot.artifact_sha256)
    assert evidence["reference_entries"]
    assert evidence["source_epoch_uids"]
    assert evidence["metric_output_sha256"] == canonical_hash(evidence["metric_output"])


def test_metric_runner_replays_the_legacy_producer_unchanged(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("HISTORICAL_REPLAY")

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
        metric_producer_version=MarketMetricProducer.LEGACY_RULE_VERSION,
    )

    assert result.producer_version == MarketMetricProducer.LEGACY_RULE_VERSION


def test_replay_same_sealed_snapshot_reuses_identical_facts_and_evidence(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if replay output depends on runtime order or wall-clock timing."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("HISTORICAL_REPLAY")
    runner = MetricRunner(runtime, writer, artifacts)

    first = runner.run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )
    second = runner.run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    assert second.evidence_sha256 == first.evidence_sha256
    assert second.guardian_fact_uids == first.guardian_fact_uids
    assert second.scout_fact_uids == first.scout_fact_uids


def test_metric_runner_refuses_official_snapshot_before_cr003(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if CR-004 accidentally becomes an OFFICIAL execution path."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("OFFICIAL")

    with pytest.raises(MetricRunnerDispositionError, match="non-OFFICIAL"):
        MetricRunner(runtime, writer, artifacts).run(
            snapshot_uid,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
        )


def test_metric_runner_marks_missing_required_provider_capability_unfit(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[..., str],
) -> None:
    """Break if complete bars/blocks can bypass CR-004 capability fitness gating."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW", complete_capabilities=False)

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    assert result.quality.fitness_status == "UNFIT"
    assert result.guardian_fact_uids == ()
    assert result.scout_fact_uids == ()


def test_metric_runner_rejects_non_calendar_same_clock_history(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[..., str],
) -> None:
    """Break if five intraday-like dates can stand in for five valid trading days."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot(
        "SHADOW",
        same_clock_dates=("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-22"),
    )

    with pytest.raises(MetricInputLineageError, match="previous valid trading days"):
        MetricRunner(runtime, writer, artifacts).run(
            snapshot_uid,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
        )


def test_metric_runner_refuses_overlap_before_loading_another_snapshot(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Break if concurrent work queues a stale metric evaluation."""
    runtime, writer, artifacts = m3_runtime
    runner = MetricRunner(runtime, writer, artifacts)
    assert runner._lock.acquire(blocking=False)
    try:
        with pytest.raises(MetricRunInProgressError):
            runner.run(
                "not-loaded",
                guardian_threshold_version="guardian-v1",
                scout_threshold_version="scout-v1",
            )
    finally:
        runner._lock.release()


def test_metric_snapshot_and_runner_batch_lineage_reads(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break if a 5,216-style manifest exceeds SQLite's bind-variable limit."""
    import market_monitor_analysis.metric_runner as metric_runner_module
    import market_monitor_analysis.snapshots as snapshots_module

    monkeypatch.setattr(snapshots_module, "_SQL_READ_BATCH_SIZE", 2)
    monkeypatch.setattr(metric_runner_module, "_SQL_READ_BATCH_SIZE", 2)
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW")

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    assert result.guardian_fact_uids
