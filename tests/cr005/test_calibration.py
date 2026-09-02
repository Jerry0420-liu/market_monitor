"""CR-005 deterministic replay/Shadow threshold-calibration evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from market_monitor_analysis.calibration import CalibrationInputError, CalibrationRunner
from market_monitor_analysis.canonical import canonical_bytes
from market_monitor_analysis.market_metrics import MetricQuality
from market_monitor_analysis.metric_runner import MetricRunResult
from market_monitor_analysis.thresholds import ThresholdRegistry, ThresholdUnavailableError
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

AS_OF = datetime(2026, 8, 24, 1, 45, tzinfo=UTC)
_GUARDIAN_VERSION = "guardian-thresholds-v1.0-prod"
_SCOUT_VERSION = "scout-thresholds-v1.0-prod"
_OFFICIAL_TABLES = (
    "current_state_projection",
    "capability_watermark",
    "current_event_projection",
    "notification_delivery_state",
    "market_event",
    "notification_intent",
    "delivery_attempt",
)


def test_calibration_reports_counts_quality_phase_and_conflicts_without_activation(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Passing calibration writes evidence but never activates production thresholds."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)

    result = CalibrationRunner(artifacts, registry).evaluate(
        (
            _metric_run(registry, artifacts, "one", "CONTINUOUS_AM", guardian=("RISE_RATE_PPM",)),
            _metric_run(registry, artifacts, "two", "BREAK"),
            _metric_run(
                registry,
                artifacts,
                "three",
                "CONTINUOUS_PM",
                scout=("EARLY_ACTIVITY_PPM",),
            ),
            _metric_run(
                registry,
                artifacts,
                "four",
                "CLOSED",
                fitness="FIT_WITH_LIMITATIONS",
            ),
        ),
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert result.passed is True
    assert result.guardian_trigger_counts["RISING_TOO_FAST"] == 1
    assert result.scout_trigger_counts["EARLY_ACTIVITY"] == 1
    assert result.conflicts == ()
    assert result.phase_counts["BREAK"] == 1
    assert result.quality_counts["FIT_WITH_LIMITATIONS"] == 1
    assert result.failure_codes == ()
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_validation").scalar() == 2
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_activation").scalar() == 0


def test_calibration_uses_guardian_quality_effects_not_reason_code_strings(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Calibration aggregates the same quality protection effect as Guardian."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    runs = (
        _metric_run(registry, artifacts, "allow", "CONTINUOUS_AM"),
        _metric_run(
            registry,
            artifacts,
            "limited",
            "CONTINUOUS_AM",
            fitness="FIT_WITH_LIMITATIONS",
            reason_codes=("OPAQUE_LIMITATION",),
        ),
        _metric_run(registry, artifacts, "paused", "CONTINUOUS_AM", fitness="UNFIT"),
        _metric_run(
            registry,
            artifacts,
            "downgraded",
            "CONTINUOUS_AM",
            guardian=("RISE_RATE_PPM",),
        ),
        _metric_run(
            registry,
            artifacts,
            "suppressed",
            "CONTINUOUS_AM",
            guardian=("BREADTH_COLLAPSE_PPM",),
        ),
    )

    result = CalibrationRunner(artifacts, registry).evaluate(
        runs,
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert result.guardian_effect_counts == {
        "ALLOW": 1,
        "ALLOW_WITH_WARNING": 1,
        "DOWNGRADE": 1,
        "SUPPRESS": 1,
        "PAUSE": 1,
    }
    assert result.data_limitation_count == 2
    assert result.guardian_pause_count == 1
    assert result.guardian_trigger_counts["RISING_TOO_FAST"] == 1
    assert result.guardian_trigger_counts["BREADTH_COLLAPSING"] == 1
    with pytest.raises(ThresholdUnavailableError):
        registry.resolve_official("GUARDIAN", AS_OF)


def test_calibration_rejects_always_on_and_unsafe_scout_results(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Pathological threshold behavior must fail calibration before activation."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    always_on = tuple(
        _metric_run(
            registry,
            artifacts,
            f"rise-{index}",
            "CONTINUOUS_AM",
            guardian=("RISE_RATE_PPM",),
        )
        for index in range(5)
    )
    unsafe = _metric_run(
        registry,
        artifacts,
        "unsafe",
        "CONTINUOUS_PM",
        guardian=("BREADTH_COLLAPSE_PPM",),
        scout=("EARLY_ACTIVITY_PPM",),
        fitness="UNFIT",
    )

    result = CalibrationRunner(artifacts, registry).evaluate(
        (*always_on, unsafe),
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="SHADOW",
        observed_at=AS_OF,
    )

    assert result.passed is False
    assert "GUARDIAN_ALWAYS_ON" in result.failure_codes
    assert "GUARDIAN_ON_UNFIT_INPUT" in result.failure_codes
    assert "SCOUT_ON_UNFIT_INPUT" in result.failure_codes
    assert "SCOUT_WHILE_GUARDIAN_SUPPRESSED" in result.failure_codes
    assert result.conflicts == ("unsafe",)
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_activation").scalar() == 0
    with pytest.raises(ThresholdUnavailableError):
        registry.resolve_official("SCOUT", AS_OF)


def test_calibration_cannot_change_official_projections_or_delivery_state(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Calibration is evidence-only even when it writes PASS validation rows."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    before = _official_counts(runtime)

    CalibrationRunner(artifacts, registry).evaluate(
        (_metric_run(registry, artifacts, "isolated", "CONTINUOUS_AM"),),
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert _official_counts(runtime) == before


def test_calibration_reports_scout_rate_quality_and_noncontinuous_phase_failures(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """The remaining automatable CR-005 gates are evidence, not silent pass-throughs."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    runs = (
        _metric_run(
            registry, artifacts, "scout-one", "CONTINUOUS_AM", scout=("EARLY_ACTIVITY_PPM",)
        ),
        _metric_run(
            registry, artifacts, "scout-two", "CONTINUOUS_PM", scout=("EARLY_ACTIVITY_PPM",)
        ),
        _metric_run(
            registry, artifacts, "scout-three", "CONTINUOUS_AM", scout=("EARLY_ACTIVITY_PPM",)
        ),
        _metric_run(registry, artifacts, "baseline", "CONTINUOUS_PM"),
        _metric_run(
            registry,
            artifacts,
            "limited",
            "CONTINUOUS_PM",
            scout=("EARLY_ACTIVITY_PPM",),
            fitness="FIT_WITH_LIMITATIONS",
        ),
        _metric_run(registry, artifacts, "lunch", "BREAK", guardian=("RISE_RATE_PPM",)),
    )

    result = CalibrationRunner(artifacts, registry).evaluate(
        runs,
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="SHADOW",
        observed_at=AS_OF,
    )

    assert result.passed is False
    assert "SCOUT_ALWAYS_ON" in result.failure_codes
    assert "SCOUT_ON_LIMITED_INPUT" in result.failure_codes
    assert "THRESHOLD_TRIGGER_OUTSIDE_CONTINUOUS_SESSION" in result.failure_codes
    assert result.data_limitation_count == 1
    assert result.guardian_downgrade_count == 1
    assert result.guardian_suppression_count == 0


def test_calibration_flags_false_triggers_for_noncontinuous_trading_clock_phases(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Pre-open, lunch break, and close cannot create a threshold signal by themselves."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    result = CalibrationRunner(artifacts, registry).evaluate(
        tuple(
            _metric_run(registry, artifacts, phase.lower(), phase, guardian=("RISE_RATE_PPM",))
            for phase in ("PRE_OPEN", "BREAK", "CLOSED")
        ),
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert "THRESHOLD_TRIGGER_OUTSIDE_CONTINUOUS_SESSION" in result.failure_codes
    assert result.phase_counts == {"BREAK": 1, "CLOSED": 1, "PRE_OPEN": 1}


def test_repeated_calibration_reuses_its_immutable_validation_artifact(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Same inputs and as-of time must be idempotent without an activation row."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    runs = (
        _metric_run(registry, artifacts, "first", "CONTINUOUS_AM"),
        _metric_run(registry, artifacts, "second", "CONTINUOUS_PM"),
    )

    first = CalibrationRunner(artifacts, registry).evaluate(
        runs,
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )
    second = CalibrationRunner(artifacts, registry).evaluate(
        tuple(reversed(runs)),
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert first.evidence_sha256 == second.evidence_sha256
    assert first.guardian_never_triggered
    assert first.scout_never_triggered
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_validation").scalar() == 2
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM threshold_activation").scalar() == 0


def test_calibration_rejects_unknown_persisted_metric_status(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Only CR-004 VALUE/MISSING/NOT_APPLICABLE statuses can enter calibration."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    malformed = _metric_run(
        registry,
        artifacts,
        "malformed",
        "CONTINUOUS_AM",
        invalid_metric_status=True,
    )

    with pytest.raises(CalibrationInputError, match="status"):
        CalibrationRunner(artifacts, registry).evaluate(
            (malformed,),
            guardian_version=_GUARDIAN_VERSION,
            scout_version=_SCOUT_VERSION,
            validation_kind="REPLAY",
            observed_at=AS_OF,
        )


def test_calibration_replays_an_explicit_legacy_threshold_version(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Replay can use its recorded legacy version without consulting active production state."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    legacy = _metric_run(
        registry,
        artifacts,
        "legacy",
        "CONTINUOUS_AM",
        guardian_version="guardian-v1",
        scout_version="scout-v1",
    )

    result = CalibrationRunner(artifacts, registry).evaluate(
        (legacy,),
        guardian_version="guardian-v1",
        scout_version="scout-v1",
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert result.guardian_threshold_version == "guardian-v1"
    assert result.scout_threshold_version == "scout-v1"
    with pytest.raises(ThresholdUnavailableError):
        registry.resolve_official("GUARDIAN", AS_OF)


def test_calibration_fails_never_on_rules_in_labeled_representative_samples(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """A stressed/strong replay label makes a missing required signal a failed calibration."""
    runtime, writer, artifacts = m3_runtime
    registry = ThresholdRegistry(runtime, writer)
    representative = _metric_run(
        registry,
        artifacts,
        "representative",
        "CONTINUOUS_AM",
        stressed_guardian=("RISE_RATE_PPM",),
        strong_broad_scout=("EARLY_ACTIVITY_PPM",),
    )

    result = CalibrationRunner(artifacts, registry).evaluate(
        (representative,),
        guardian_version=_GUARDIAN_VERSION,
        scout_version=_SCOUT_VERSION,
        validation_kind="REPLAY",
        observed_at=AS_OF,
    )

    assert result.passed is False
    assert "GUARDIAN_NEVER_ON_STRESSED" in result.failure_codes
    assert "SCOUT_NEVER_ON_STRONG_BROAD" in result.failure_codes
    assert result.guardian_never_on_stressed == ("RISING_TOO_FAST",)
    assert result.scout_never_on_strong_broad == ("EARLY_ACTIVITY",)


def _metric_run(
    registry: ThresholdRegistry,
    artifacts: ArtifactStore,
    snapshot_uid: str,
    phase: str,
    *,
    guardian: tuple[str, ...] = (),
    scout: tuple[str, ...] = (),
    fitness: Literal["FIT", "FIT_WITH_LIMITATIONS", "UNFIT"] = "FIT",
    reason_codes: tuple[str, ...] | None = None,
    invalid_metric_status: bool = False,
    guardian_version: str = _GUARDIAN_VERSION,
    scout_version: str = _SCOUT_VERSION,
    stressed_guardian: tuple[str, ...] = (),
    strong_broad_scout: tuple[str, ...] = (),
) -> MetricRunResult:
    """Store the immutable evidence a calibrator is required to inspect."""
    guardian_set = registry.resolve_explicit("GUARDIAN", guardian_version, AS_OF)
    scout_set = registry.resolve_explicit("SCOUT", scout_version, AS_OF)
    if reason_codes is None:
        reason_codes = ("DATA_LIMITATION",) if fitness == "FIT_WITH_LIMITATIONS" else ()
    metrics = [
        _metric_value(code, value, guardian, scout, fitness, set(guardian_set.entries))
        for code, value in sorted({**guardian_set.entries, **scout_set.entries}.items())
    ]
    if invalid_metric_status:
        metrics[0] = {**metrics[0], "status": "UNKNOWN", "value_ppm": None}
    document = {
        "schema_version": 1,
        "snapshot_uid": snapshot_uid,
        "producer_version": "cr004-market-metrics-v1",
        "guardian_threshold": {
            "uid": guardian_set.uid,
            "version": guardian_version,
            "definition_hash": guardian_set.definition_hash,
        },
        "scout_threshold": {
            "uid": scout_set.uid,
            "version": scout_version,
            "definition_hash": scout_set.definition_hash,
        },
        "input_lineage": {"market_phase": phase},
        "calibration_context": {
            "stressed_guardian_metrics": list(stressed_guardian),
            "strong_broad_scout_metrics": list(strong_broad_scout),
        },
        "metric_output": {
            "quality": {
                "fitness_status": fitness,
                "quote_coverage_ppm": 1_000_000,
                "historical_coverage_ppm": 1_000_000,
                "valid_member_count": 5,
                "tradable_expected_count": 5,
                "reason_codes": list(reason_codes),
            },
            "metrics": metrics,
        },
        "guardian_threshold_matches": list(guardian),
        "scout_threshold_matches": list(scout),
    }
    artifact = artifacts.put_bytes(
        canonical_bytes(document),
        "application/vnd.market-monitor.cr004-metric-evidence+json",
    )
    artifacts.register(artifact)
    return MetricRunResult(
        snapshot_uid=snapshot_uid,
        producer_version="cr004-market-metrics-v1",
        evidence_sha256=artifact.sha256,
        guardian_fact_uids=(),
        scout_fact_uids=(),
        guardian_threshold_matches=guardian,
        scout_threshold_matches=scout,
        quality=MetricQuality(
            fitness_status=fitness,
            quote_coverage_ppm=1_000_000,
            historical_coverage_ppm=1_000_000,
            valid_member_count=5,
            tradable_expected_count=5,
            reason_codes=reason_codes,
        ),
        elapsed_ms=1,
        evaluation_time=AS_OF,
    )


def _metric_value(
    code: str,
    threshold: int,
    guardian: tuple[str, ...],
    scout: tuple[str, ...],
    fitness: str,
    guardian_codes: set[str],
) -> dict[str, int | str | None]:
    if code in (*guardian, *scout):
        return {"code": code, "status": "VALUE", "value_ppm": threshold}
    if fitness == "UNFIT":
        return {"code": code, "status": "MISSING", "value_ppm": None}
    if fitness == "FIT_WITH_LIMITATIONS" and code not in guardian_codes:
        return {"code": code, "status": "NOT_APPLICABLE", "value_ppm": None}
    return {"code": code, "status": "VALUE", "value_ppm": threshold - 1}


def _official_counts(runtime: DatabaseRuntime) -> dict[str, int]:
    with runtime.read_connection() as connection:
        counts: dict[str, int] = {}
        for table in _OFFICIAL_TABLES:
            value = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
            assert value is not None
            counts[table] = int(value)
        return counts
