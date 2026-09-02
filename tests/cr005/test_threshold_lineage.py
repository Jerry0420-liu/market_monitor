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
from market_monitor_analysis.facts import FactExecutor
from market_monitor_analysis.guardian import GuardianService
from market_monitor_analysis.scout import ScoutService
from market_monitor_analysis.state import StateService
from market_monitor_analysis.thresholds import ThresholdLineageError

from tests.m6.test_analysis_commit import _bound_request, _request, _sealed_snapshot
from tests.support.thresholds import activate_test_threshold_pair, metric_evidence

AS_OF = datetime(2026, 8, 4, tzinfo=UTC)
PRODUCER_VERSION = "cr004-market-metrics-v1"


def _provenance(artifacts: Any) -> MetricProvenance:
    return MetricProvenance(PRODUCER_VERSION, metric_evidence(artifacts))


def _counts(runtime: Any) -> dict[str, int]:
    tables = (
        "analysis_commit",
        "current_state_projection",
        "market_event",
        "current_event_projection",
        "notification_intent",
        "notification_delivery_state",
        "delivery_attempt",
    )
    with runtime.read_connection() as connection:
        return {
            table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
            for table in tables
        }


def _metrics(entries: Any, trigger_code: str | None = None) -> dict[str, int]:
    values = dict.fromkeys(entries, 0)
    if trigger_code is not None:
        values[trigger_code] = entries[trigger_code]
    return values


def _state_with_metric_facts(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    guardian_entries: Any,
    guardian_uid: str,
    scout_entries: Any,
    scout_uid: str,
) -> str:
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts, disposition="SHADOW")
    facts = FactExecutor(runtime, writer)
    guardian_facts = facts.record_metrics(
        snapshot_uid,
        "GUARDIAN_INPUTS",
        PRODUCER_VERSION,
        _metrics(guardian_entries, "BREADTH_COLLAPSE_PPM"),
        threshold_version_uid=guardian_uid,
    )
    scout_facts = facts.record_scout_metrics(
        snapshot_uid,
        "SCOUT_INPUTS",
        PRODUCER_VERSION,
        _metrics(scout_entries, "EARLY_ACTIVITY_PPM"),
        threshold_version_uid=scout_uid,
    )
    return (
        StateService(runtime, writer)
        .evaluate(
            snapshot_uid,
            "AVAILABLE",
            "OBSERVING",
            [fact.fact_uid for fact in (*guardian_facts, *scout_facts)],
            0,
        )
        .evaluation_uid
    )


def test_rule_execution_keeps_producer_and_threshold_versions_separate(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", AS_OF)
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts, disposition="SHADOW")

    evidence_sha256 = metric_evidence(artifacts)
    FactExecutor(runtime, writer).record_metrics(
        snapshot_uid,
        "GUARDIAN_INPUTS",
        PRODUCER_VERSION,
        {"RISE_RATE_PPM": guardian.entries["RISE_RATE_PPM"]},
        threshold_version_uid=guardian.uid,
        evidence_sha256=evidence_sha256,
    )

    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT rule_version,threshold_version_uid,output_hash FROM rule_execution "
            "WHERE snapshot_uid=? AND rule_key='GUARDIAN_INPUTS'",
            (snapshot_uid,),
        ).one()
    assert tuple(row) == (PRODUCER_VERSION, guardian.uid, evidence_sha256)
    assert row.output_hash != guardian.definition_hash


def test_guardian_and_scout_read_thresholds_from_fact_lineage(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", AS_OF)
    scout = registry.resolve_explicit("SCOUT", "scout-thresholds-v1.0-prod", AS_OF)
    state_uid = _state_with_metric_facts(
        runtime, writer, artifacts, guardian.entries, guardian.uid, scout.entries, scout.uid
    )

    guardian_result = GuardianService(runtime, writer).evaluate(state_uid)
    scout_result = ScoutService(runtime, writer).evaluate(guardian_result.guardian_uid)

    assert guardian_result.guardian_effect == "SUPPRESS"
    assert any(item.risk_tag == "BREADTH_COLLAPSING" for item in guardian_result.risks)
    assert scout_result.suppressed_by_guardian
    assert any(item.opportunity_tag == "EARLY_ACTIVITY" for item in scout_result.tags)
    with runtime.read_connection() as connection:
        versions = (
            connection.exec_driver_sql(
                "SELECT rule_version FROM guardian_evaluation UNION ALL "
                "SELECT rule_version FROM scout_evaluation"
            )
            .scalars()
            .all()
        )
    assert versions == ["guardian-v1", "scout-v1"]


def test_guardian_and_scout_reject_mixed_threshold_fact_lineage(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", AS_OF)
    scout = registry.resolve_explicit("SCOUT", "scout-thresholds-v1.0-prod", AS_OF)
    mixed_guardian = registry.register_version(
        "GUARDIAN",
        "guardian-mixed-fixture",
        "OWNER_APPROVED",
        "test-owner",
        guardian.entries,
        AS_OF,
    )
    mixed_scout = registry.register_version(
        "SCOUT", "scout-mixed-fixture", "OWNER_APPROVED", "test-owner", scout.entries, AS_OF
    )
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts, disposition="SHADOW")
    facts = FactExecutor(runtime, writer)
    guardian_values = _metrics(guardian.entries)
    guardian_values["RISE_RATE_PPM"] = guardian.entries["RISE_RATE_PPM"]
    guardian_facts = facts.record_metrics(
        snapshot_uid,
        "GUARDIAN_INPUTS",
        PRODUCER_VERSION,
        guardian_values,
        threshold_version_uid=guardian.uid,
    )
    replacement_guardian = facts.record_metrics(
        snapshot_uid,
        "GUARDIAN_INPUTS",
        PRODUCER_VERSION,
        {"RISE_RATE_PPM": guardian.entries["RISE_RATE_PPM"]},
        threshold_version_uid=mixed_guardian.uid,
    )
    scout_facts = facts.record_scout_metrics(
        snapshot_uid,
        "SCOUT_INPUTS",
        PRODUCER_VERSION,
        _metrics(scout.entries),
        threshold_version_uid=scout.uid,
    )
    state_uid = (
        StateService(runtime, writer)
        .evaluate(
            snapshot_uid,
            "AVAILABLE",
            "OBSERVING",
            [
                *[fact.fact_uid for fact in guardian_facts if fact.fact_code != "RISE_RATE_PPM"],
                *[fact.fact_uid for fact in replacement_guardian],
                *[fact.fact_uid for fact in scout_facts],
            ],
            0,
        )
        .evaluation_uid
    )
    with pytest.raises(ThresholdLineageError, match="one threshold version"):
        GuardianService(runtime, writer).evaluate(state_uid)

    scout_snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts, disposition="SHADOW")
    guardian_facts = facts.record_metrics(
        scout_snapshot_uid,
        "GUARDIAN_INPUTS",
        PRODUCER_VERSION,
        _metrics(guardian.entries),
        threshold_version_uid=guardian.uid,
    )
    scout_facts = facts.record_scout_metrics(
        scout_snapshot_uid,
        "SCOUT_INPUTS",
        PRODUCER_VERSION,
        _metrics(scout.entries),
        threshold_version_uid=scout.uid,
    )
    replacement_scout = facts.record_scout_metrics(
        scout_snapshot_uid,
        "SCOUT_INPUTS",
        PRODUCER_VERSION,
        {"EARLY_ACTIVITY_PPM": scout.entries["EARLY_ACTIVITY_PPM"]},
        threshold_version_uid=mixed_scout.uid,
    )
    scout_state_uid = (
        StateService(runtime, writer)
        .evaluate(
            scout_snapshot_uid,
            "AVAILABLE",
            "OBSERVING",
            [
                *[fact.fact_uid for fact in guardian_facts],
                *[fact.fact_uid for fact in scout_facts if fact.fact_code != "EARLY_ACTIVITY_PPM"],
                *[fact.fact_uid for fact in replacement_scout],
            ],
            0,
        )
        .evaluation_uid
    )
    clean_guardian = GuardianService(runtime, writer).evaluate(scout_state_uid)
    with pytest.raises(ThresholdLineageError, match="one threshold version"):
        ScoutService(runtime, writer).evaluate(clean_guardian.guardian_uid)


def test_official_commit_without_active_validated_pair_has_no_side_effects(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts)
    before = _counts(runtime)

    with pytest.raises(OfficialThresholdGateError):
        AnalysisCommitService(runtime, writer).commit(
            replace(_request(runtime, snapshot_uid), metric_provenance=_provenance(artifacts))
        )

    assert _counts(runtime) == before


def test_official_commit_records_active_pair_and_metric_provenance(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = activate_test_threshold_pair(runtime, writer, artifacts, AS_OF)
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts)
    request = _bound_request(runtime, writer, artifacts, snapshot_uid)
    assert request.metric_provenance is not None
    result = AnalysisCommitService(runtime, writer).commit(request)

    guardian = registry.resolve_official("GUARDIAN", datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC))
    scout = registry.resolve_official("SCOUT", datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC))
    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "SELECT rule_key,rule_version,threshold_version_uid,output_hash FROM rule_execution "
            "WHERE snapshot_uid=? AND rule_key IN "
            "('CR004_GUARDIAN_METRICS','CR004_SCOUT_METRICS') "
            "ORDER BY rule_key",
            (snapshot_uid,),
        ).all()
    assert [tuple(row) for row in rows] == [
        (
            "CR004_GUARDIAN_METRICS",
            PRODUCER_VERSION,
            guardian.uid,
            request.metric_provenance.evidence_sha256,
        ),
        (
            "CR004_SCOUT_METRICS",
            PRODUCER_VERSION,
            scout.uid,
            request.metric_provenance.evidence_sha256,
        ),
    ]
    assert result.event_version_uids
