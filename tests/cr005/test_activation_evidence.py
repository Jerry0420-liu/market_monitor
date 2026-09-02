"""C3 production activation evidence is typed, verified, and fail-closed."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.thresholds import ThresholdActivationError, ThresholdRegistry
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.values import format_rfc3339

AS_OF = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
_GUARDIAN = "guardian-thresholds-v1.0-prod"
_SCOUT = "scout-thresholds-v1.0-prod"


def test_activation_requires_verified_typed_production_evidence(
    m3_runtime: tuple[Any, Any, ArtifactStore],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    _record_generic_passes(registry, artifacts)

    with pytest.raises(ThresholdActivationError):
        registry.activate_pair(
            _GUARDIAN,
            _SCOUT,
            AS_OF,
            "owner-acceptance-typed",
            acceptance_evidence_sha256="0" * 64,
        )

    evidence_sha256 = _activation_evidence(registry, artifacts)
    assert registry.record_activation_evidence(evidence_sha256)
    with artifacts.open_verified(evidence_sha256) as stream:
        replay_evidence_sha256 = json.load(stream)["replay_result"]["evidence_sha256"]
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT replay_evidence_sha256 FROM threshold_activation_evidence "
            "WHERE evidence_sha256=?",
            (evidence_sha256,),
        ).one()
    assert row.replay_evidence_sha256 == replay_evidence_sha256
    first = registry.activate_pair(
        _GUARDIAN,
        _SCOUT,
        AS_OF + timedelta(minutes=1),
        "owner-acceptance-typed",
        acceptance_evidence_sha256=evidence_sha256,
    )
    second = registry.activate_pair(
        _GUARDIAN,
        _SCOUT,
        AS_OF + timedelta(minutes=1),
        "owner-acceptance-typed",
        acceptance_evidence_sha256=evidence_sha256,
    )

    assert second == first
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT acceptance_evidence_uid FROM threshold_activation WHERE activation_uid=?",
            (first[0],),
        ).one()
    assert row.acceptance_evidence_uid


@pytest.mark.parametrize(
    "mutate",
    (
        lambda document: document.__setitem__("evidence_origin", "FIXTURE"),
        lambda document: document["observations"].pop(),
        lambda document: document["observations"].__setitem__(1, document["observations"][0]),
        lambda document: document["observations"][0].__setitem__("market_phase", "BREAK"),
        lambda document: document["observations"][0].pop("input_manifest_hash"),
        lambda document: document["side_effect_audit"].__setitem__(
            "official_business_side_effect_delta", 1
        ),
        lambda document: document["replay_result"].__setitem__("passed", False),
    ),
)
def test_activation_evidence_rejects_fixture_or_incomplete_production_proof(
    m3_runtime: tuple[Any, Any, ArtifactStore],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    document = _activation_document(registry, artifacts)
    mutate(document)
    artifact = artifacts.put_bytes(
        canonical_bytes(document),
        "application/vnd.market-monitor.cr005-production-activation-evidence+json",
    )
    artifacts.register(artifact)

    with pytest.raises(ThresholdActivationError):
        registry.record_activation_evidence(artifact.sha256)


def test_activation_evidence_cannot_be_used_before_its_observation_time(
    m3_runtime: tuple[Any, Any, ArtifactStore],
) -> None:
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    _record_generic_passes(registry, artifacts)
    evidence_sha256 = _activation_evidence(registry, artifacts)
    registry.record_activation_evidence(evidence_sha256)

    with pytest.raises(ThresholdActivationError):
        registry.activate_pair(
            _GUARDIAN,
            _SCOUT,
            AS_OF - timedelta(seconds=1),
            "owner-acceptance-too-early",
            acceptance_evidence_sha256=evidence_sha256,
        )


def _activation_evidence(registry: ThresholdRegistry, artifacts: ArtifactStore) -> str:
    artifact = artifacts.put_bytes(
        canonical_bytes(_activation_document(registry, artifacts)),
        "application/vnd.market-monitor.cr005-production-activation-evidence+json",
    )
    artifacts.register(artifact)
    return artifact.sha256


def _activation_document(registry: ThresholdRegistry, artifacts: ArtifactStore) -> dict[str, Any]:
    guardian = registry.resolve_explicit("GUARDIAN", _GUARDIAN, AS_OF)
    scout = registry.resolve_explicit("SCOUT", _SCOUT, AS_OF)
    replay = artifacts.put_bytes(
        canonical_bytes(
            {
                "schema_version": 2,
                "evidence_kind": "CR005_CALIBRATION",
                "validation_kind": "REPLAY",
                "guardian_threshold": _threshold_document(guardian),
                "scout_threshold": _threshold_document(scout),
                "passed": True,
                "failure_codes": [],
            }
        ),
        "application/vnd.market-monitor.cr005-calibration+json",
    )
    artifacts.register(replay)
    observations = [
        {
            "observation_uid": f"round-{index:02d}",
            "snapshot_uid": f"snapshot-{index:02d}",
            "snapshot_hash": canonical_hash({"snapshot": index}),
            "input_manifest_hash": canonical_hash({"manifest": index}),
            "metric_evidence_sha256": canonical_hash({"metric": index}),
            "producer_version": "cr004-market-metrics-v2",
            "market_phase": "CONTINUOUS_AM",
            "quality_status": "FIT",
        }
        for index in range(20)
    ]
    observation_ids = [item["observation_uid"] for item in observations]
    lineages = [
        (item["snapshot_uid"], item["snapshot_hash"], item["input_manifest_hash"])
        for item in observations
    ]
    return {
        "schema_version": 1,
        "evidence_kind": "CR005_PRODUCTION_ACTIVATION",
        "evidence_origin": "LIVE_SHADOW",
        "validation_kind": "SHADOW",
        "observed_at": format_rfc3339(AS_OF),
        "guardian_threshold": _threshold_document(guardian),
        "scout_threshold": _threshold_document(scout),
        "metric_producer_version": "cr004-market-metrics-v2",
        "observations": observations,
        "observation_identity_hash": canonical_hash(observation_ids),
        "snapshot_input_lineage_hash": canonical_hash(lineages),
        "quality_distribution": {"FIT": 20, "FIT_WITH_LIMITATIONS": 0, "UNFIT": 0},
        "sanity_result": {"passed": True, "failure_codes": []},
        "side_effect_audit": {"passed": True, "official_business_side_effect_delta": 0},
        "replay_result": {"passed": True, "evidence_sha256": replay.sha256},
    }


def _record_generic_passes(registry: ThresholdRegistry, artifacts: ArtifactStore) -> None:
    for family, version in (("GUARDIAN", _GUARDIAN), ("SCOUT", _SCOUT)):
        for kind in ("REPLAY", "SHADOW"):
            artifact = artifacts.put_bytes(
                f"fixture:{family}:{version}:{kind}".encode(), "application/json"
            )
            artifacts.register(artifact)
            registry.record_validation(
                family,
                version,
                kind,
                "PASS",
                f"fixture:{artifact.sha256}",
                artifact.sha256,
                AS_OF,
            )


def _threshold_document(threshold: Any) -> dict[str, str]:
    return {
        "uid": threshold.uid,
        "version": threshold.version,
        "definition_hash": threshold.definition_hash,
    }
