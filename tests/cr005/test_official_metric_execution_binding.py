from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_analysis.analysis_commit import (
    AnalysisCommitService,
    MetricProvenance,
    OfficialThresholdGateError,
)
from market_monitor_analysis.thresholds import ThresholdRegistry

from tests.m6.test_analysis_commit import (
    _bound_request,
    _metric_evidence,
    _request,
    _sealed_snapshot,
)
from tests.support.thresholds import activate_test_threshold_pair, metric_evidence

AS_OF = datetime(2026, 8, 4, tzinfo=UTC)
PRODUCER_VERSION = "cr004-market-metrics-v1"


def test_official_commit_rejects_caller_metrics_without_matching_cr004_executions(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts)
    base = _request(runtime, snapshot_uid)
    registry = ThresholdRegistry(runtime, writer)
    request = replace(
        base,
        metric_provenance=MetricProvenance(
            PRODUCER_VERSION,
            _metric_evidence(
                runtime,
                artifacts,
                snapshot_uid,
                PRODUCER_VERSION,
                registry.resolve_official("GUARDIAN", AS_OF),
                registry.resolve_official("SCOUT", AS_OF),
                base.guardian_metrics,
                base.scout_metrics,
            ),
        ),
    )

    with pytest.raises(OfficialThresholdGateError, match="CR004"):
        AnalysisCommitService(runtime, writer).commit(request)

    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM analysis_commit").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT count(*) FROM fact_record").scalar_one() == 0


def test_official_commit_rejects_caller_metric_values_that_differ_from_persisted_execution(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    request = _bound_request(
        runtime,
        writer,
        artifacts,
        _sealed_snapshot(runtime, writer, artifacts)[0],
    )
    substituted = replace(
        request,
        guardian_metrics={**request.guardian_metrics, "RISE_RATE_PPM": 1_000_000},
    )

    with pytest.raises(OfficialThresholdGateError, match="metric"):
        AnalysisCommitService(runtime, writer).commit(substituted)

    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM analysis_commit").scalar_one() == 0


def test_official_commit_reuses_persisted_cr004_facts_for_scout_evidence(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    request = _bound_request(
        runtime,
        writer,
        artifacts,
        _sealed_snapshot(runtime, writer, artifacts)[0],
    )

    AnalysisCommitService(runtime, writer).commit(request)

    with runtime.read_connection() as connection:
        rule_keys = (
            connection.exec_driver_sql(
                "SELECT DISTINCT execution.rule_key "
                "FROM scout_opportunity_evidence evidence "
                "JOIN fact_record fact ON fact.fact_uid=evidence.fact_uid "
                "JOIN rule_execution execution "
                "ON execution.rule_execution_uid=fact.producer_rule_execution_uid "
                "ORDER BY execution.rule_key"
            )
            .scalars()
            .all()
        )
    assert rule_keys == ["CR004_SCOUT_METRICS"]


def test_official_commit_rejects_cr004_facts_with_unbound_metric_artifact(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    request = replace(
        _bound_request(
            runtime,
            writer,
            artifacts,
            _sealed_snapshot(runtime, writer, artifacts)[0],
        ),
        metric_provenance=MetricProvenance(PRODUCER_VERSION, metric_evidence(artifacts)),
    )

    with pytest.raises(OfficialThresholdGateError, match="lineage"):
        AnalysisCommitService(runtime, writer).commit(request)
