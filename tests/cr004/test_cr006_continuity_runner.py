"""CR-006 runner evidence selection."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.market_metrics import MarketMetricProducer
from market_monitor_analysis.metric_runner import MetricRunner
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue


def test_shadow_runner_uses_only_prior_same_subject_v2_continuity_evidence(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if CR-006 can consume future, other-subject, or legacy evidence."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW")
    with runtime.read_connection() as connection:
        current = connection.exec_driver_sql(
            "SELECT s.subject_uid,s.manifest_uid,s.bundle_uid,s.quality_context_uid,"
            "s.as_of_time,b.canonical_hash AS bundle_hash "
            "FROM evaluation_snapshot s JOIN reference_version_bundle b "
            "ON b.bundle_uid=s.bundle_uid WHERE s.snapshot_uid=?",
            (snapshot_uid,),
        ).one()
    current_time = datetime.fromisoformat(str(current.as_of_time).replace("Z", "+00:00"))

    def seed(
        observed_at: datetime,
        *,
        subject_uid: str,
        rule_version: str = MarketMetricProducer.RULE_VERSION,
    ) -> str:
        prior_uid = new_uid()
        snapshot_hash = canonical_hash({"snapshot_uid": prior_uid})
        metric_output = {
            "rule_version": rule_version,
            "quality": {"fitness_status": "FIT"},
            "base_facts": {
                "sector_return_5m": "0.020",
                "market_return_5m": "0.010",
                "breadth_5m_up": "0.700",
                "turnover_ratio_5m": "1.200",
            },
        }
        evidence = {
            "schema_version": 2,
            "snapshot_uid": prior_uid,
            "snapshot_hash": snapshot_hash,
            "subject_uid": subject_uid,
            "bundle_hash": str(current.bundle_hash),
            "producer_version": rule_version,
            "metric_output": metric_output,
            "metric_output_sha256": canonical_hash(metric_output),
        }
        artifact = artifacts.put_bytes(
            canonical_bytes(evidence),
            "application/vnd.market-monitor.cr004-metric-evidence+json",
        )
        artifacts.register(artifact)
        observed_at_text = format_rfc3339(observed_at)

        def command(transaction: TransactionContext) -> None:
            connection = transaction.connection
            connection.exec_driver_sql(
                "INSERT INTO evaluation_snapshot("
                "snapshot_uid,subject_uid,manifest_uid,bundle_uid,quality_context_uid,"
                "as_of_time,max_skew_ms,evaluation_disposition,snapshot_status,"
                "canonical_hash,created_at,sealed_at) VALUES (?,?,?,?,?,?,0,"
                "'SHADOW','SEALED',?,?,?)",
                (
                    prior_uid,
                    subject_uid,
                    str(current.manifest_uid),
                    str(current.bundle_uid),
                    str(current.quality_context_uid),
                    observed_at_text,
                    snapshot_hash,
                    observed_at_text,
                    observed_at_text,
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO rule_execution("
                "rule_execution_uid,snapshot_uid,rule_key,rule_version,"
                "validation_status,input_hash,output_hash,started_at,finished_at,"
                "threshold_version_uid) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                (
                    new_uid(),
                    prior_uid,
                    "CR004_GUARDIAN_METRICS",
                    rule_version,
                    "VALID",
                    canonical_hash(("cr006-continuity-test", prior_uid)),
                    artifact.sha256,
                    observed_at_text,
                    observed_at_text,
                ),
            )

        writer.submit(command).result()
        return prior_uid

    first_uid = seed(current_time - timedelta(minutes=14), subject_uid=str(current.subject_uid))
    second_uid = seed(current_time - timedelta(minutes=5), subject_uid=str(current.subject_uid))
    seed(current_time + timedelta(minutes=5), subject_uid=str(current.subject_uid))
    references = ReferenceRepository(runtime, writer)
    other_sector_uid = references.create_sector("CONCEPT", current_time - timedelta(days=1))
    other_subject_uid = references.ensure_analysis_subject("SECTOR", other_sector_uid)
    seed(current_time - timedelta(minutes=1), subject_uid=other_subject_uid)
    seed(
        current_time - timedelta(minutes=2),
        subject_uid=str(current.subject_uid),
        rule_version=MarketMetricProducer.LEGACY_RULE_VERSION,
    )

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    with artifacts.open_verified(result.evidence_sha256) as stream:
        evidence = json.load(stream)
    assert [
        item["observation_uid"]
        for item in evidence["metric_output"]["base_facts"]["continuity_observations"]
    ] == [first_uid, second_uid, snapshot_uid]
