from __future__ import annotations

from datetime import datetime

from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.thresholds import ThresholdRegistry
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import WriterQueue


def activate_test_threshold_pair(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    at: datetime,
    *,
    acceptance_id: str = "test-threshold-acceptance",
) -> ThresholdRegistry:
    registry = ThresholdRegistry(runtime, writer)
    for family, version in (
        ("GUARDIAN", "guardian-thresholds-v1.0-prod"),
        ("SCOUT", "scout-thresholds-v1.0-prod"),
    ):
        for validation_kind in ("REPLAY", "SHADOW"):
            artifact = artifacts.put_bytes(
                f"{family}:{version}:{validation_kind}".encode(), "application/json"
            )
            artifacts.register(artifact)
            registry.record_validation(
                family,
                version,
                validation_kind,
                "PASS",
                f"test-artifact:{artifact.sha256}",
                artifact.sha256,
                at,
            )
    evidence_sha256 = production_shaped_test_activation_evidence(registry, artifacts, at)
    registry.activate_pair(
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        at,
        acceptance_id,
        acceptance_evidence_sha256=evidence_sha256,
    )
    return registry


def production_shaped_test_activation_evidence(
    registry: ThresholdRegistry, artifacts: ArtifactStore, at: datetime
) -> str:
    """Build a schema-conformant artifact for tests of the production gate."""
    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", at)
    scout = registry.resolve_explicit("SCOUT", "scout-thresholds-v1.0-prod", at)
    guardian_reference = _threshold_reference(guardian)
    scout_reference = _threshold_reference(scout)
    replay = artifacts.put_bytes(
        canonical_bytes(
            {
                "schema_version": 2,
                "evidence_kind": "CR005_CALIBRATION",
                "validation_kind": "REPLAY",
                "guardian_threshold": guardian_reference,
                "scout_threshold": scout_reference,
                "passed": True,
                "failure_codes": [],
            }
        ),
        "application/vnd.market-monitor.cr005-calibration+json",
    )
    artifacts.register(replay)
    observations = [
        {
            "observation_uid": f"test-round-{index:02d}",
            "snapshot_uid": f"test-snapshot-{index:02d}",
            "snapshot_hash": canonical_hash({"snapshot": index}),
            "input_manifest_hash": canonical_hash({"manifest": index}),
            "metric_evidence_sha256": canonical_hash({"metric": index}),
            "producer_version": "cr004-market-metrics-v2",
            "market_phase": "CONTINUOUS_AM",
            "quality_status": "FIT",
        }
        for index in range(20)
    ]
    artifact = artifacts.put_bytes(
        canonical_bytes(
            {
                "schema_version": 1,
                "evidence_kind": "CR005_PRODUCTION_ACTIVATION",
                "evidence_origin": "LIVE_SHADOW",
                "validation_kind": "SHADOW",
                "observed_at": format_rfc3339(at),
                "guardian_threshold": guardian_reference,
                "scout_threshold": scout_reference,
                "metric_producer_version": "cr004-market-metrics-v2",
                "observations": observations,
                "observation_identity_hash": canonical_hash(
                    [item["observation_uid"] for item in observations]
                ),
                "snapshot_input_lineage_hash": canonical_hash(
                    [
                        (
                            item["snapshot_uid"],
                            item["snapshot_hash"],
                            item["input_manifest_hash"],
                        )
                        for item in observations
                    ]
                ),
                "quality_distribution": {"FIT": 20, "FIT_WITH_LIMITATIONS": 0, "UNFIT": 0},
                "sanity_result": {"passed": True, "failure_codes": []},
                "side_effect_audit": {
                    "passed": True,
                    "official_business_side_effect_delta": 0,
                },
                "replay_result": {"passed": True, "evidence_sha256": replay.sha256},
            }
        ),
        "application/vnd.market-monitor.cr005-production-activation-evidence+json",
    )
    artifacts.register(artifact)
    registry.record_activation_evidence(artifact.sha256)
    return artifact.sha256


def _threshold_reference(threshold: object) -> dict[str, str]:
    return {
        "uid": str(getattr(threshold, "uid")),
        "version": str(getattr(threshold, "version")),
        "definition_hash": str(getattr(threshold, "definition_hash")),
    }


def metric_evidence(artifacts: ArtifactStore) -> str:
    artifact = artifacts.put_bytes(b'{"fixture":"metric-evidence"}', "application/json")
    artifacts.register(artifact)
    return artifact.sha256
