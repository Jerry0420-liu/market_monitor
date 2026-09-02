from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_analysis.thresholds import (
    ThresholdActivationError,
    ThresholdRegistry,
    ThresholdUnavailableError,
)
from market_monitor_persistence.artifacts import ArtifactStore
from sqlalchemy.exc import DatabaseError, IntegrityError

from tests.support.thresholds import production_shaped_test_activation_evidence

AS_OF = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
_GUARDIAN = {
    "RISE_RATE_PPM": 600_000,
    "HEAD_CONCENTRATION_PPM": 600_000,
    "INTERNAL_DIVERGENCE_PPM": 550_000,
    "CROWDING_PPM": 600_000,
    "LIQUIDITY_WEAKENING_PPM": 550_000,
    "CORE_WEAKENING_PPM": 550_000,
    "BREADTH_COLLAPSE_PPM": 550_000,
    "STAMPEDE_RISK_PPM": 600_000,
    "T1_CHASING_RISK_PPM": 600_000,
    "EARLY_SIGNAL_FAILURE_PPM": 550_000,
}
_SCOUT = {
    "EARLY_ACTIVITY_PPM": 650_000,
    "HEALTHY_BREADTH_PPM": 650_000,
    "RELATIVE_STRENGTH_PPM": 650_000,
    "TURNOVER_CONFIRMATION_PPM": 600_000,
    "ETF_CONFIRMATION_PPM": 550_000,
    "STYLE_SUPPORT_PPM": 550_000,
    "LOW_CROWDING_PPM": 650_000,
    "CONTINUITY_STRENGTHENING_PPM": 650_000,
}


def test_registry_seeds_owner_versions_and_legacy_replay_values(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m3_runtime
    registry = ThresholdRegistry(runtime, writer)

    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", AS_OF)
    scout = registry.resolve_explicit("SCOUT", "scout-thresholds-v1.0-prod", AS_OF)
    legacy_guardian = registry.resolve_explicit("GUARDIAN", "guardian-v1", AS_OF)
    legacy_scout = registry.resolve_explicit("SCOUT", "scout-v1", AS_OF)

    assert guardian.entries == _GUARDIAN
    assert scout.entries == _SCOUT
    assert legacy_guardian.entries["RISE_RATE_PPM"] == 700_000
    assert legacy_guardian.entries["LIQUIDITY_WEAKENING_PPM"] == 500_000
    assert legacy_scout.entries == {code: 600_000 for code in _SCOUT}
    with pytest.raises(ThresholdUnavailableError):
        registry.resolve_official("GUARDIAN", AS_OF)


def test_activation_is_append_only_idempotent_and_resolves_one_version_as_of_time(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    evidence_sha256 = _validate_pair(registry, runtime, writer)

    first = registry.activate_pair(
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        AS_OF,
        "owner-acceptance-1",
        acceptance_evidence_sha256=evidence_sha256,
    )
    second = registry.activate_pair(
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        AS_OF,
        "owner-acceptance-1",
        acceptance_evidence_sha256=evidence_sha256,
    )

    assert second == first
    assert registry.resolve_official("GUARDIAN", AS_OF + timedelta(seconds=1)).version == (
        "guardian-thresholds-v1.0-prod"
    )
    assert registry.resolve_official("SCOUT", AS_OF + timedelta(seconds=1)).version == (
        "scout-thresholds-v1.0-prod"
    )
    with pytest.raises(ThresholdActivationError):
        registry.activate_pair(
            "guardian-thresholds-v1.0-prod",
            "scout-thresholds-v1.0-prod",
            AS_OF,
            "owner-acceptance-2",
            acceptance_evidence_sha256=evidence_sha256,
        )

    store = ArtifactStore(runtime, writer)
    failed_report = store.put_bytes(b"later shadow failure", "application/json")
    store.register(failed_report)
    registry.record_validation(
        "GUARDIAN",
        "guardian-thresholds-v1.0-prod",
        "SHADOW",
        "FAIL",
        f"artifact:{failed_report.sha256}",
        failed_report.sha256,
        AS_OF + timedelta(minutes=2),
    )
    assert registry.resolve_official("GUARDIAN", AS_OF + timedelta(minutes=1)).version == (
        "guardian-thresholds-v1.0-prod"
    )
    with pytest.raises(ThresholdUnavailableError):
        registry.resolve_official("GUARDIAN", AS_OF + timedelta(minutes=3))


def test_registry_rejects_incomplete_unknown_mixed_or_unvalidated_activation(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", AS_OF)

    with pytest.raises(ValueError, match="complete"):
        registry.register_version(
            "GUARDIAN",
            "guardian-incomplete-test",
            "OWNER_APPROVED",
            "test-owner",
            {"RISE_RATE_PPM": 1},
            AS_OF,
        )
    with pytest.raises(ValueError, match="unknown"):
        registry.register_version(
            "SCOUT",
            "scout-unknown-test",
            "OWNER_APPROVED",
            "test-owner",
            {**_SCOUT, "UNKNOWN_PPM": 1},
            AS_OF,
        )
    with pytest.raises(ThresholdActivationError):
        registry.activate_pair(
            "guardian-v1",
            "scout-v1",
            AS_OF,
            "legacy-not-official",
            acceptance_evidence_sha256="0" * 64,
        )
    with pytest.raises(ThresholdActivationError):
        registry.activate_pair(
            "guardian-thresholds-v1.0-prod",
            "scout-thresholds-v1.0-prod",
            AS_OF,
            "unvalidated-prod",
            acceptance_evidence_sha256="0" * 64,
        )
    with runtime._writer_engine.begin() as connection:
        with pytest.raises((DatabaseError, IntegrityError)):
            connection.exec_driver_sql(
                "INSERT INTO threshold_activation"
                "(activation_uid,family,threshold_version_uid,effective_at,idempotency_key,"
                "owner_approval_reference,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    "00000000-0000-4000-8000-000000009991",
                    "GUARDIAN",
                    guardian.uid,
                    "2026-08-24T01:30:00.000000Z",
                    "direct-unvalidated",
                    "test-owner",
                    "2026-08-24T01:30:00.000000Z",
                ),
            )


def test_registry_rows_are_database_append_only_and_deduplicated(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    evidence_sha256 = _validate_pair(registry, runtime, writer)
    registry.activate_pair(
        "guardian-thresholds-v1.0-prod",
        "scout-thresholds-v1.0-prod",
        AS_OF,
        "owner-acceptance-1",
        acceptance_evidence_sha256=evidence_sha256,
    )
    guardian = registry.resolve_explicit("GUARDIAN", "guardian-thresholds-v1.0-prod", AS_OF)

    with runtime._writer_engine.begin() as connection:
        with pytest.raises((DatabaseError, IntegrityError)):
            connection.exec_driver_sql(
                "UPDATE threshold_version SET version='rewritten' WHERE threshold_version_uid=?",
                (guardian.uid,),
            )
        with pytest.raises((DatabaseError, IntegrityError)):
            connection.exec_driver_sql(
                "DELETE FROM threshold_entry WHERE threshold_version_uid=?",
                (guardian.uid,),
            )
        with pytest.raises((DatabaseError, IntegrityError)):
            connection.exec_driver_sql(
                "UPDATE threshold_validation SET validation_status='FAIL' "
                "WHERE threshold_version_uid=?",
                (guardian.uid,),
            )
        with pytest.raises((DatabaseError, IntegrityError)):
            connection.exec_driver_sql(
                "INSERT INTO threshold_entry"
                "(threshold_entry_uid,threshold_version_uid,metric_code,threshold_ppm) "
                "VALUES ('00000000-0000-4000-8000-000000009999',?,?,1)",
                (guardian.uid, "RISE_RATE_PPM"),
            )
        with pytest.raises((DatabaseError, IntegrityError)):
            connection.exec_driver_sql(
                "DELETE FROM threshold_activation WHERE family='GUARDIAN'",
            )


def _validate_pair(registry: ThresholdRegistry, runtime: Any, writer: Any) -> str:
    store = ArtifactStore(runtime, writer)
    for family, version in (
        ("GUARDIAN", "guardian-thresholds-v1.0-prod"),
        ("SCOUT", "scout-thresholds-v1.0-prod"),
    ):
        for kind in ("REPLAY", "SHADOW"):
            artifact = store.put_bytes(
                f"{family}:{version}:{kind}".encode(),
                "application/json",
            )
            store.register(artifact)
            registry.record_validation(
                family,
                version,
                kind,
                "PASS",
                f"artifact:{artifact.sha256}",
                artifact.sha256,
                AS_OF,
            )
    return production_shaped_test_activation_evidence(registry, store, AS_OF)
