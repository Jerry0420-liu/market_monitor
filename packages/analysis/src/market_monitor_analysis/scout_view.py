from dataclasses import dataclass

from market_monitor_analysis.scout import ScoutEvaluation


@dataclass(frozen=True)
class ScoutReasonView:
    opportunity_tag: str
    reason_code: str
    fact_uid: str


@dataclass(frozen=True)
class ScoutView:
    status: str
    strength: str
    suppressed_by_guardian: bool
    guardian_uid: str
    guardian_effect: str
    suppression_reason: str | None
    reasons: tuple[ScoutReasonView, ...]


def map_scout_view(evaluation: ScoutEvaluation) -> ScoutView:
    return ScoutView(
        evaluation.scout_status,
        evaluation.scout_strength,
        evaluation.suppressed_by_guardian,
        evaluation.guardian_uid,
        evaluation.guardian_effect,
        "SUPPRESSED_BY_GUARDIAN" if evaluation.suppressed_by_guardian else None,
        tuple(
            ScoutReasonView(item.opportunity_tag, item.reason_code, item.primary_fact_uid)
            for item in evaluation.tags
        ),
    )
