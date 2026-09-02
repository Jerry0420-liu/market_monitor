"""CR-006 Shadow audit surface must distinguish business effects from references."""

from __future__ import annotations

from collections.abc import Callable

from market_monitor_analysis.metric_runner import MetricRunner
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

from scripts.tdx_runner import _audit_surface_counts, _side_effect_audit


def test_shadow_audit_surface_covers_every_official_business_boundary(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    runtime, writer, artifacts = m3_runtime
    before = _audit_surface_counts(runtime)

    MetricRunner(runtime, writer, artifacts).run(
        sealed_metric_snapshot("SHADOW"),
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    audit = _side_effect_audit(before, _audit_surface_counts(runtime))

    assert audit["schema_version"] == 1
    assert audit["passed"] is True
    assert audit["official_business_side_effect_delta"] == 0
    assert set(audit["official_business"]["delta"]) == {
        "analysis_commit",
        "capability_watermark",
        "current_event_projection",
        "current_state_projection",
        "delivery_attempt",
        "event_evidence",
        "event_version",
        "guardian_evaluation",
        "market_event",
        "notification_delivery_state",
        "notification_intent",
        "scout_evaluation",
        "state_evaluation",
        "threshold_activation",
    }
    assert all(value == 0 for value in audit["official_business"]["delta"].values())
    assert audit["reference_onboarding"]["classification"] == "NON_BUSINESS_REFERENCE"


def test_shadow_audit_fails_closed_when_an_official_boundary_changes() -> None:
    before = {
        "official_business": {"analysis_commit": 0},
        "reference_onboarding": {"analysis_subject": 2},
    }
    after = {
        "official_business": {"analysis_commit": 1},
        "reference_onboarding": {"analysis_subject": 3},
    }

    audit = _side_effect_audit(before, after)

    assert audit["passed"] is False
    assert audit["official_business_side_effect_delta"] == 1
    assert audit["official_business"]["delta"] == {"analysis_commit": 1}
    assert audit["reference_onboarding"]["delta"] == {"analysis_subject": 1}
