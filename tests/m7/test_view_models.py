import pytest
from market_monitor_contracts.models import (
    ConfidenceView,
    DataQualityView,
    EvidenceItemView,
    ExplanationView,
    GuardianView,
    LastValidStateView,
    MarketView,
    ScoutView,
    confidence_from_quality,
    decode_cursor,
    encode_cursor,
)
from pydantic import ValidationError


def _market(availability: str, lifecycle: str | None) -> MarketView:
    confidence = confidence_from_quality(availability, "HEALTHY", "FIT", "HIGH", "VALID")
    return MarketView(
        subject_uid="00000000-0000-4000-8000-000000000001",
        availability_state=availability,
        lifecycle_state=lifecycle,
        lifecycle_value_status="VALUE" if lifecycle is not None else "NOT_APPLICABLE",
        as_of_time="2026-08-04T00:00:00Z",
        confidence=confidence,
        guardian=GuardianView(status="NORMAL", effect="ALLOW", blocking=False, reasons=[]),
        scout=ScoutView(status="NONE", strength="LOW", suppressed_by_guardian=False, reasons=[]),
        explanation=ExplanationView(supporting=[], contrary=[]),
        data_quality=DataQualityView(
            health="HEALTHY",
            fitness="FIT",
            evidence_sufficiency="HIGH",
            coverage=1,
            limitations=[],
        ),
        last_valid_state=LastValidStateView(
            lifecycle_state=None,
            as_of_time=None,
            value_status="MISSING",
        ),
        source={"evaluation_uid": "00000000-0000-4000-8000-000000000002"},
    )


def test_market_view_enforces_availability_lifecycle_and_stable_public_shape() -> None:
    assert _market("AVAILABLE", "OBSERVING").lifecycle_state == "OBSERVING"
    suspended = _market("SUSPENDED", None)
    assert suspended.lifecycle_state is None
    assert "subject_uid" in suspended.model_dump(mode="json")
    with pytest.raises(ValidationError):
        _market("SUSPENDED", "OBSERVING")


@pytest.mark.parametrize(
    ("availability", "health", "fitness", "evidence", "validation", "expected"),
    [
        ("AVAILABLE", "HEALTHY", "FIT", "HIGH", "VALID", ("HIGH", "NORMAL_REFERENCE")),
        (
            "AVAILABLE",
            "HEALTHY",
            "FIT",
            "MEDIUM",
            "VALID",
            ("MEDIUM", "LIMITED_REFERENCE"),
        ),
        (
            "AVAILABLE",
            "DEGRADED",
            "FIT_WITH_LIMITATIONS",
            "LOW",
            "VALID",
            ("LOW", "OBSERVATION_ONLY"),
        ),
        (
            "AVAILABLE",
            "UNHEALTHY",
            "UNFIT",
            "HIGH",
            "VALID",
            ("BLOCKED", "NO_JUDGMENT"),
        ),
        (
            "SUSPENDED",
            "HEALTHY",
            "FIT",
            "HIGH",
            "VALID",
            ("BLOCKED", "NO_JUDGMENT"),
        ),
        (
            "AVAILABLE",
            "HEALTHY",
            "FIT",
            "HIGH",
            "INVALID",
            ("BLOCKED", "NO_JUDGMENT"),
        ),
        (
            "AVAILABLE",
            "HEALTHY",
            "FIT",
            "HIGH",
            "UNFIT",
            ("BLOCKED", "NO_JUDGMENT"),
        ),
    ],
)
def test_confidence_is_deterministic_quality_mapping(
    availability: str,
    health: str,
    fitness: str,
    evidence: str,
    validation: str,
    expected: tuple[str, str],
) -> None:
    value: ConfidenceView = confidence_from_quality(
        availability, health, fitness, evidence, validation
    )
    assert (value.level, value.reference_status) == expected


def test_nullable_view_values_require_explicit_value_status_and_provenance() -> None:
    evidence = EvidenceItemView(
        role="SUPPORTING",
        reason_code="FIXTURE_REASON",
        fact_uid=None,
        fact_uid_status="MISSING",
        rule_execution_uid=None,
        rule_execution_uid_status="MISSING",
        template_key="FIXTURE_REASON",
        attributes={"source": "fixture"},
    )
    assert evidence.fact_uid_status == "MISSING"
    with pytest.raises(ValidationError):
        EvidenceItemView(
            role="SUPPORTING",
            reason_code="FIXTURE_REASON",
            fact_uid=None,
            fact_uid_status="VALUE",
            rule_execution_uid=None,
            rule_execution_uid_status="MISSING",
            template_key="FIXTURE_REASON",
            attributes={},
        )


def test_cursor_is_opaque_round_trippable_and_rejects_tampering() -> None:
    cursor = encode_cursor("2026-08-04T00:00:00Z", "00000000-0000-4000-8000-000000000001")
    assert "2026" not in cursor
    assert decode_cursor(cursor) == (
        "2026-08-04T00:00:00Z",
        "00000000-0000-4000-8000-000000000001",
    )
    with pytest.raises(ValueError, match="cursor"):
        decode_cursor(cursor + "broken")
