"""CR-005 append-only calibration evidence for persisted CR-004 metric runs."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, cast

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.values import format_rfc3339

from market_monitor_analysis.canonical import canonical_bytes
from market_monitor_analysis.guardian import (
    guardian_decision,
    guardian_rule_metadata,
    guardian_threshold_matches,
)
from market_monitor_analysis.metric_runner import MetricRunResult
from market_monitor_analysis.scout import scout_rule_metadata, scout_threshold_matches
from market_monitor_analysis.thresholds import ThresholdRegistry

_CONTINUOUS_PHASES = frozenset({"CONTINUOUS_AM", "CONTINUOUS_PM"})
_EFFECT_ORDER = {
    "ALLOW": 0,
    "ALLOW_WITH_WARNING": 1,
    "DOWNGRADE": 2,
    "SUPPRESS": 3,
    "PAUSE": 4,
}


class CalibrationError(RuntimeError):
    """Base error for CR-005 calibration evidence."""


class CalibrationInputError(CalibrationError):
    """Raised when a persisted metric artifact cannot prove its lineage."""


@dataclass(frozen=True)
class CalibrationResult:
    """Immutable aggregate used by CR-004/CR-005 joint acceptance."""

    evidence_sha256: str
    guardian_threshold_version: str
    scout_threshold_version: str
    validation_kind: Literal["REPLAY", "SHADOW"]
    metric_run_count: int
    producer_versions: tuple[str, ...]
    guardian_trigger_counts: Mapping[str, int]
    scout_trigger_counts: Mapping[str, int]
    guardian_effect_counts: Mapping[str, int]
    guardian_suppression_count: int
    guardian_downgrade_count: int
    guardian_pause_count: int
    quality_counts: Mapping[str, int]
    phase_counts: Mapping[str, int]
    data_limitation_count: int
    unfit_count: int
    conflicts: tuple[str, ...]
    guardian_never_triggered: tuple[str, ...]
    scout_never_triggered: tuple[str, ...]
    guardian_stressed_sample_counts: Mapping[str, int]
    scout_strong_broad_sample_counts: Mapping[str, int]
    guardian_never_on_stressed: tuple[str, ...]
    scout_never_on_strong_broad: tuple[str, ...]
    failure_codes: tuple[str, ...]
    passed: bool


class CalibrationRunner:
    """Aggregate sealed Shadow/Replay metric evidence without activation."""

    def __init__(self, artifacts: ArtifactStore, registry: ThresholdRegistry) -> None:
        self._artifacts = artifacts
        self._registry = registry

    def evaluate(
        self,
        metric_runs: Sequence[MetricRunResult],
        *,
        guardian_version: str,
        scout_version: str,
        validation_kind: Literal["REPLAY", "SHADOW"],
        observed_at: datetime,
    ) -> CalibrationResult:
        if validation_kind not in {"REPLAY", "SHADOW"}:
            raise ValueError("validation kind must be REPLAY or SHADOW")
        if not metric_runs:
            raise ValueError("calibration requires at least one persisted metric run")

        guardian = self._registry.resolve_explicit("GUARDIAN", guardian_version, observed_at)
        scout = self._registry.resolve_explicit("SCOUT", scout_version, observed_at)
        guardian_metadata = _guardian_metadata(guardian.entries)
        scout_metadata = _scout_metadata(scout.entries)
        guardian_counts: Counter[str] = Counter({tag: 0 for tag, _ in guardian_metadata.values()})
        scout_counts: Counter[str] = Counter({tag: 0 for tag in scout_metadata.values()})
        effect_counts: Counter[str] = Counter({effect: 0 for effect in _EFFECT_ORDER})
        quality_counts: Counter[str] = Counter({"FIT": 0, "FIT_WITH_LIMITATIONS": 0, "UNFIT": 0})
        phase_counts: Counter[str] = Counter()
        stressed_counts: Counter[str] = Counter({code: 0 for code in guardian.entries})
        stressed_match_counts: Counter[str] = Counter({code: 0 for code in guardian.entries})
        strong_broad_counts: Counter[str] = Counter({code: 0 for code in scout.entries})
        strong_broad_match_counts: Counter[str] = Counter({code: 0 for code in scout.entries})
        failure_codes: set[str] = set()
        conflicts: list[str] = []
        producer_versions: set[str] = set()
        data_limitation_count = 0
        unfit_count = 0
        fit_count = 0

        ordered_runs = sorted(metric_runs, key=lambda run: (run.evidence_sha256, run.snapshot_uid))
        for run in ordered_runs:
            (
                phase,
                raw_guardian_matches,
                scout_matches,
                stressed_guardian,
                strong_broad_scout,
                guardian_values,
            ) = self._verified_matches(run, guardian, scout)
            producer_versions.add(run.producer_version)
            quality = run.quality
            quality_counts[quality.fitness_status] += 1
            phase_counts[phase] += 1
            unfit_count += int(quality.fitness_status == "UNFIT")
            fit_count += int(quality.fitness_status == "FIT")

            decision = guardian_decision(
                guardian_values,
                guardian.entries,
                availability_state="AVAILABLE",
                data_health_status="HEALTHY",
                fitness_status=quality.fitness_status,
            )
            guardian_matches = decision.threshold_matches
            data_limitation_count += int(
                any(item.risk_tag == "DATA_LIMITATION" for item in decision.risks)
            )

            for code in guardian_matches:
                tag, _ = guardian_metadata[code]
                guardian_counts[tag] += 1
                stressed_match_counts[code] += int(code in stressed_guardian)
            for code in scout_matches:
                scout_counts[scout_metadata[code]] += 1
                strong_broad_match_counts[code] += int(code in strong_broad_scout)
            for code in stressed_guardian:
                stressed_counts[code] += 1
            for code in strong_broad_scout:
                strong_broad_counts[code] += 1

            effect = decision.guardian_effect
            effect_counts[effect] += 1
            if scout_matches and (guardian_matches or effect in {"SUPPRESS", "PAUSE"}):
                conflicts.append(run.snapshot_uid)
            if scout_matches and quality.fitness_status == "UNFIT":
                failure_codes.add("SCOUT_ON_UNFIT_INPUT")
            if scout_matches and quality.fitness_status == "FIT_WITH_LIMITATIONS":
                failure_codes.add("SCOUT_ON_LIMITED_INPUT")
            if raw_guardian_matches and quality.fitness_status == "UNFIT":
                failure_codes.add("GUARDIAN_ON_UNFIT_INPUT")
            if scout_matches and effect in {"SUPPRESS", "PAUSE"}:
                failure_codes.add("SCOUT_WHILE_GUARDIAN_SUPPRESSED")
            if (guardian_matches or scout_matches) and phase not in _CONTINUOUS_PHASES:
                failure_codes.add("THRESHOLD_TRIGGER_OUTSIDE_CONTINUOUS_SESSION")

        if fit_count == 0:
            failure_codes.add("NO_FIT_EVALUATIONS")
        else:
            if any(count * 100 > fit_count * 80 for count in guardian_counts.values()):
                failure_codes.add("GUARDIAN_ALWAYS_ON")
            if any(count * 100 > fit_count * 60 for count in scout_counts.values()):
                failure_codes.add("SCOUT_ALWAYS_ON")

        guardian_never = tuple(sorted(tag for tag, count in guardian_counts.items() if count == 0))
        scout_never = tuple(sorted(tag for tag, count in scout_counts.items() if count == 0))
        guardian_never_on_stressed = tuple(
            sorted(
                guardian_metadata[code][0]
                for code, count in stressed_counts.items()
                if count and stressed_match_counts[code] == 0
            )
        )
        scout_never_on_strong_broad = tuple(
            sorted(
                scout_metadata[code]
                for code, count in strong_broad_counts.items()
                if count and strong_broad_match_counts[code] == 0
            )
        )
        if guardian_never_on_stressed:
            failure_codes.add("GUARDIAN_NEVER_ON_STRESSED")
        if scout_never_on_strong_broad:
            failure_codes.add("SCOUT_NEVER_ON_STRONG_BROAD")
        failures = tuple(sorted(failure_codes))
        document = {
            "schema_version": 2,
            "evidence_kind": "CR005_CALIBRATION",
            "validation_kind": validation_kind,
            "observed_at": format_rfc3339(observed_at),
            "guardian_threshold": _threshold_document(guardian),
            "scout_threshold": _threshold_document(scout),
            "metric_runs": [
                {
                    "snapshot_uid": run.snapshot_uid,
                    "evidence_sha256": run.evidence_sha256,
                    "producer_version": run.producer_version,
                }
                for run in ordered_runs
            ],
            "guardian_trigger_counts": dict(sorted(guardian_counts.items())),
            "scout_trigger_counts": dict(sorted(scout_counts.items())),
            "guardian_effect_counts": dict(sorted(effect_counts.items())),
            "quality_counts": dict(sorted(quality_counts.items())),
            "phase_counts": dict(sorted(phase_counts.items())),
            "data_limitation_count": data_limitation_count,
            "unfit_count": unfit_count,
            "conflicts": tuple(sorted(conflicts)),
            "guardian_never_triggered": guardian_never,
            "scout_never_triggered": scout_never,
            "guardian_stressed_sample_counts": dict(sorted(stressed_counts.items())),
            "scout_strong_broad_sample_counts": dict(sorted(strong_broad_counts.items())),
            "guardian_never_on_stressed": guardian_never_on_stressed,
            "scout_never_on_strong_broad": scout_never_on_strong_broad,
            "failure_codes": failures,
            "passed": not failures,
        }
        artifact = self._artifacts.put_bytes(
            canonical_bytes(document),
            "application/vnd.market-monitor.cr005-calibration+json",
        )
        self._artifacts.register(artifact)
        status = "PASS" if not failures else "FAIL"
        for family, version in (("GUARDIAN", guardian.version), ("SCOUT", scout.version)):
            self._registry.record_validation(
                family,
                version,
                validation_kind,
                status,
                f"artifact:{artifact.sha256}",
                artifact.sha256,
                observed_at,
            )
        return CalibrationResult(
            evidence_sha256=artifact.sha256,
            guardian_threshold_version=guardian.version,
            scout_threshold_version=scout.version,
            validation_kind=validation_kind,
            metric_run_count=len(metric_runs),
            producer_versions=tuple(sorted(producer_versions)),
            guardian_trigger_counts=_readonly_counts(guardian_counts),
            scout_trigger_counts=_readonly_counts(scout_counts),
            guardian_effect_counts=_readonly_counts(effect_counts),
            guardian_suppression_count=effect_counts["SUPPRESS"],
            guardian_downgrade_count=effect_counts["DOWNGRADE"],
            guardian_pause_count=effect_counts["PAUSE"],
            quality_counts=_readonly_counts(quality_counts),
            phase_counts=_readonly_counts(phase_counts),
            data_limitation_count=data_limitation_count,
            unfit_count=unfit_count,
            conflicts=tuple(sorted(conflicts)),
            guardian_never_triggered=guardian_never,
            scout_never_triggered=scout_never,
            guardian_stressed_sample_counts=_readonly_counts(stressed_counts),
            scout_strong_broad_sample_counts=_readonly_counts(strong_broad_counts),
            guardian_never_on_stressed=guardian_never_on_stressed,
            scout_never_on_strong_broad=scout_never_on_strong_broad,
            failure_codes=failures,
            passed=not failures,
        )

    def _verified_matches(
        self,
        run: MetricRunResult,
        guardian: Any,
        scout: Any,
    ) -> tuple[
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        dict[str, int],
    ]:
        document = self._document(run)
        if document.get("schema_version") not in (1, 2):
            raise CalibrationInputError("unsupported CR-004 metric evidence schema")
        if document.get("snapshot_uid") != run.snapshot_uid:
            raise CalibrationInputError("metric evidence snapshot lineage mismatch")
        if document.get("producer_version") != run.producer_version:
            raise CalibrationInputError("metric evidence producer version mismatch")
        _verify_threshold(document, "guardian_threshold", guardian)
        _verify_threshold(document, "scout_threshold", scout)

        lineage = _mapping(document.get("input_lineage"), "input_lineage")
        phase = lineage.get("market_phase")
        if not isinstance(phase, str) or not phase:
            raise CalibrationInputError("metric evidence market phase is required")
        output = _mapping(document.get("metric_output"), "metric_output")
        _verify_quality(output, run)
        values = _metric_values(output, set(guardian.entries) | set(scout.entries))
        guardian_matches = guardian_threshold_matches(
            {code: values[code] for code in guardian.entries if code in values}, guardian.entries
        )
        scout_matches = scout_threshold_matches(
            {code: values[code] for code in scout.entries if code in values}, scout.entries
        )
        documented_guardian = _string_tuple(
            document.get("guardian_threshold_matches"), "guardian matches"
        )
        if documented_guardian != guardian_matches:
            raise CalibrationInputError("metric evidence Guardian threshold result mismatch")
        documented_scout = _string_tuple(document.get("scout_threshold_matches"), "scout matches")
        if documented_scout != scout_matches:
            raise CalibrationInputError("metric evidence Scout threshold result mismatch")
        if run.guardian_threshold_matches != guardian_matches:
            raise CalibrationInputError("persisted Guardian match lineage mismatch")
        if run.scout_threshold_matches != scout_matches:
            raise CalibrationInputError("persisted Scout match lineage mismatch")
        context = _mapping(document.get("calibration_context", {}), "calibration_context")
        stressed_guardian = _metric_code_tuple(
            context.get("stressed_guardian_metrics", []),
            "stressed Guardian metrics",
            set(guardian.entries),
        )
        strong_broad_scout = _metric_code_tuple(
            context.get("strong_broad_scout_metrics", []),
            "strong/broad Scout metrics",
            set(scout.entries),
        )
        return (
            phase,
            guardian_matches,
            scout_matches,
            stressed_guardian,
            strong_broad_scout,
            {code: values[code] for code in guardian.entries if code in values},
        )

    def _document(self, run: MetricRunResult) -> dict[str, Any]:
        try:
            with self._artifacts.open_verified(run.evidence_sha256) as stream:
                value = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise CalibrationInputError("cannot read metric evidence artifact") from error
        return _mapping(value, "metric evidence")


def _guardian_metadata(entries: Mapping[str, int]) -> dict[str, tuple[str, str]]:
    metadata: dict[str, tuple[str, str]] = {}
    for code in entries:
        item = guardian_rule_metadata(code)
        if item is None:
            raise CalibrationInputError(f"unknown Guardian metric {code}")
        metadata[code] = item
    return metadata


def _scout_metadata(entries: Mapping[str, int]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for code in entries:
        item = scout_rule_metadata(code)
        if item is None:
            raise CalibrationInputError(f"unknown Scout metric {code}")
        metadata[code] = item
    return metadata


def _threshold_document(threshold: Any) -> dict[str, str]:
    return {
        "uid": threshold.uid,
        "version": threshold.version,
        "definition_hash": threshold.definition_hash,
    }


def _verify_threshold(document: Mapping[str, Any], key: str, threshold: Any) -> None:
    if _mapping(document.get(key), key) != _threshold_document(threshold):
        raise CalibrationInputError(f"metric evidence {key} lineage mismatch")


def _verify_quality(output: Mapping[str, Any], run: MetricRunResult) -> None:
    quality = _mapping(output.get("quality"), "metric quality")
    expected = {
        "fitness_status": run.quality.fitness_status,
        "quote_coverage_ppm": run.quality.quote_coverage_ppm,
        "historical_coverage_ppm": run.quality.historical_coverage_ppm,
        "valid_member_count": run.quality.valid_member_count,
        "tradable_expected_count": run.quality.tradable_expected_count,
        "reason_codes": list(run.quality.reason_codes),
    }
    if quality != expected:
        raise CalibrationInputError("metric evidence quality lineage mismatch")


def _metric_values(output: Mapping[str, Any], expected_codes: set[str]) -> dict[str, int]:
    rows = output.get("metrics")
    if not isinstance(rows, list):
        raise CalibrationInputError("metric evidence values are required")
    seen: set[str] = set()
    values: dict[str, int] = {}
    for row in rows:
        item = _mapping(row, "metric value")
        code = item.get("code")
        status = item.get("status")
        value = item.get("value_ppm")
        if not isinstance(code, str) or code in seen:
            raise CalibrationInputError("metric evidence code is invalid")
        if not isinstance(status, str):
            raise CalibrationInputError("metric evidence status is invalid")
        seen.add(code)
        if status == "VALUE":
            if isinstance(value, bool) or not isinstance(value, int):
                raise CalibrationInputError("VALUE metric requires integer PPM")
            if not 0 <= value <= 1_000_000:
                raise CalibrationInputError("VALUE metric PPM is out of range")
            values[code] = value
        elif status not in {"MISSING", "NOT_APPLICABLE"}:
            raise CalibrationInputError("metric evidence status is invalid")
        elif value is not None:
            raise CalibrationInputError("non-VALUE metric cannot carry PPM")
    if seen != expected_codes:
        raise CalibrationInputError("metric evidence code set mismatch")
    return values


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CalibrationInputError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _readonly_counts(values: Mapping[str, int]) -> Mapping[str, int]:
    return MappingProxyType(dict(sorted(values.items())))


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CalibrationInputError(f"{name} must be a list of strings")
    return tuple(value)


def _metric_code_tuple(value: object, name: str, allowed: set[str]) -> tuple[str, ...]:
    codes = _string_tuple(value, name)
    if len(set(codes)) != len(codes) or not set(codes).issubset(allowed):
        raise CalibrationInputError(f"{name} must contain unique known metric codes")
    return tuple(sorted(codes))
