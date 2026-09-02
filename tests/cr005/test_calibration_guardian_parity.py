"""C2 parity for the shared Guardian quality/effect decision."""

from __future__ import annotations

import pytest
from market_monitor_analysis.guardian import guardian_decision

_GUARDIAN_CODES = (
    "RISE_RATE_PPM",
    "HEAD_CONCENTRATION_PPM",
    "INTERNAL_DIVERGENCE_PPM",
    "CROWDING_PPM",
    "LIQUIDITY_WEAKENING_PPM",
    "CORE_WEAKENING_PPM",
    "BREADTH_COLLAPSE_PPM",
    "STAMPEDE_RISK_PPM",
    "T1_CHASING_RISK_PPM",
    "EARLY_SIGNAL_FAILURE_PPM",
)


@pytest.mark.parametrize(
    ("fitness", "data_health", "missing", "trigger", "effect", "tags", "matches"),
    (
        ("FIT", "HEALTHY", None, None, "ALLOW", (), ()),
        (
            "FIT_WITH_LIMITATIONS",
            "HEALTHY",
            None,
            None,
            "ALLOW_WITH_WARNING",
            ("DATA_LIMITATION",),
            (),
        ),
        (
            "UNFIT",
            "HEALTHY",
            None,
            "RISE_RATE_PPM",
            "PAUSE",
            ("DATA_LIMITATION",),
            (),
        ),
        (
            "FIT",
            "DEGRADED",
            None,
            None,
            "ALLOW_WITH_WARNING",
            ("DATA_LIMITATION",),
            (),
        ),
        (
            "FIT",
            "HEALTHY",
            "RISE_RATE_PPM",
            None,
            "PAUSE",
            ("DATA_LIMITATION",),
            (),
        ),
        (
            "FIT",
            "HEALTHY",
            None,
            "RISE_RATE_PPM",
            "DOWNGRADE",
            ("RISING_TOO_FAST",),
            ("RISE_RATE_PPM",),
        ),
        (
            "FIT",
            "HEALTHY",
            None,
            "BREADTH_COLLAPSE_PPM",
            "SUPPRESS",
            ("BREADTH_COLLAPSING",),
            ("BREADTH_COLLAPSE_PPM",),
        ),
    ),
)
def test_guardian_decision_preserves_quality_and_effect_semantics(
    fitness: str,
    data_health: str,
    missing: str | None,
    trigger: str | None,
    effect: str,
    tags: tuple[str, ...],
    matches: tuple[str, ...],
) -> None:
    metrics = {code: 0 for code in _GUARDIAN_CODES}
    if missing is not None:
        del metrics[missing]
    if trigger is not None:
        metrics[trigger] = 1_000_000

    decision = guardian_decision(
        metrics,
        {code: 500_000 for code in _GUARDIAN_CODES},
        availability_state="AVAILABLE",
        data_health_status=data_health,
        fitness_status=fitness,
    )

    assert decision.guardian_effect == effect
    assert tuple(item.risk_tag for item in decision.risks) == tags
    assert decision.threshold_matches == matches
    assert decision.blocking is (effect in {"SUPPRESS", "PAUSE"})
