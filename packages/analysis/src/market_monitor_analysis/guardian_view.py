from dataclasses import dataclass

from market_monitor_analysis.guardian import GuardianEvaluation


@dataclass(frozen=True)
class GuardianReasonView:
    risk_tag: str
    severity: str
    reason_code: str
    fact_uid: str | None


@dataclass(frozen=True)
class GuardianView:
    status: str
    blocking: bool
    effect: str
    reasons: tuple[GuardianReasonView, ...]


def map_guardian_view(evaluation: GuardianEvaluation) -> GuardianView:
    status = {
        "ALLOW": "NORMAL",
        "ALLOW_WITH_WARNING": "CAUTION",
        "DOWNGRADE": "WARNING",
        "SUPPRESS": "BLOCKED",
        "PAUSE": "BLOCKED",
    }[evaluation.guardian_effect]
    return GuardianView(
        status,
        evaluation.blocking,
        evaluation.guardian_effect,
        tuple(
            GuardianReasonView(
                item.risk_tag, item.severity, item.reason_code, item.primary_fact_uid
            )
            for item in evaluation.risks
        ),
    )
