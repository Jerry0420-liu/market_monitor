from collections.abc import Mapping
from dataclasses import dataclass

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_analysis.thresholds import resolve_thresholds_for_facts


@dataclass(frozen=True)
class RiskFinding:
    risk_tag: str
    severity: str
    reason_code: str
    primary_fact_uid: str | None


@dataclass(frozen=True)
class GuardianEvaluation:
    guardian_uid: str
    state_evaluation_uid: str
    guardian_effect: str
    blocking: bool
    max_severity: str
    evaluation_hash: str
    risks: tuple[RiskFinding, ...]


_RULES = (
    ("RISE_RATE_PPM", "RISING_TOO_FAST", "HIGH", "DOWNGRADE"),
    ("HEAD_CONCENTRATION_PPM", "HEAD_CONCENTRATION_HIGH", "HIGH", "DOWNGRADE"),
    ("INTERNAL_DIVERGENCE_PPM", "INTERNAL_DIVERGENCE", "HIGH", "DOWNGRADE"),
    ("CROWDING_PPM", "CROWDING_INCREASING", "HIGH", "DOWNGRADE"),
    (
        "LIQUIDITY_WEAKENING_PPM",
        "LIQUIDITY_WEAKENING",
        "WARNING",
        "ALLOW_WITH_WARNING",
    ),
    ("CORE_WEAKENING_PPM", "CORE_MEMBERS_WEAKENING", "HIGH", "DOWNGRADE"),
    ("BREADTH_COLLAPSE_PPM", "BREADTH_COLLAPSING", "CRITICAL", "SUPPRESS"),
    ("STAMPEDE_RISK_PPM", "STAMPEDE_RISK", "CRITICAL", "SUPPRESS"),
    ("T1_CHASING_RISK_PPM", "T1_CHASING_RISK", "CRITICAL", "SUPPRESS"),
    (
        "EARLY_SIGNAL_FAILURE_PPM",
        "EARLY_SIGNAL_FAILED",
        "WARNING",
        "DOWNGRADE",
    ),
)
_EFFECT_ORDER = {
    "ALLOW": 0,
    "ALLOW_WITH_WARNING": 1,
    "DOWNGRADE": 2,
    "SUPPRESS": 3,
    "PAUSE": 4,
}


def guardian_threshold_matches(
    metrics: Mapping[str, int], thresholds: Mapping[str, int]
) -> tuple[str, ...]:
    """Return frozen Guardian metric codes whose versioned threshold is met."""
    return tuple(
        fact_code
        for fact_code, _, _, _ in _RULES
        if fact_code in metrics and metrics[fact_code] >= thresholds[fact_code]
    )


def guardian_rule_metadata(fact_code: str) -> tuple[str, str] | None:
    """Expose the frozen risk tag and effect for calibration evidence."""
    return next(((tag, effect) for code, tag, _, effect in _RULES if code == fact_code), None)


_SEVERITY_ORDER = {"NONE": 0, "INFO": 1, "WARNING": 2, "HIGH": 3, "CRITICAL": 4}


@dataclass(frozen=True)
class GuardianDecision:
    """Pure frozen Guardian quality and threshold outcome."""

    threshold_matches: tuple[str, ...]
    risks: tuple[RiskFinding, ...]
    guardian_effect: str
    blocking: bool
    max_severity: str
    unavailable: bool


def guardian_decision(
    metrics: Mapping[str, int],
    thresholds: Mapping[str, int] | None,
    *,
    availability_state: str,
    data_health_status: str,
    fitness_status: str,
    fact_uids: Mapping[str, str] | None = None,
) -> GuardianDecision:
    """Apply the production Guardian protection chain without persistence."""
    missing_metrics = {rule[0] for rule in _RULES} - metrics.keys()
    unavailable = (
        availability_state != "AVAILABLE"
        or data_health_status in {"UNKNOWN", "UNHEALTHY"}
        or fitness_status in {"UNKNOWN", "UNFIT"}
        or bool(missing_metrics)
    )
    limited = data_health_status == "DEGRADED" or fitness_status == "FIT_WITH_LIMITATIONS"
    findings: list[RiskFinding] = []
    effects: list[str] = []
    if unavailable or limited:
        findings.append(
            RiskFinding(
                "DATA_LIMITATION",
                "CRITICAL" if unavailable else "WARNING",
                "REQUIRED_GUARDIAN_FACTS_MISSING"
                if missing_metrics
                else "QUALITY_REQUIRES_PAUSE"
                if unavailable
                else "QUALITY_LIMITED",
                None,
            )
        )
        effects.append("PAUSE" if unavailable else "ALLOW_WITH_WARNING")

    matches: tuple[str, ...] = ()
    if not unavailable and thresholds is not None:
        matches = guardian_threshold_matches(metrics, thresholds)
        uids = {} if fact_uids is None else fact_uids
        for fact_code, risk_tag, severity, effect in _RULES:
            if fact_code in matches:
                findings.append(
                    RiskFinding(
                        risk_tag,
                        severity,
                        f"{risk_tag}_THRESHOLD_MET",
                        uids.get(fact_code),
                    )
                )
                effects.append(effect)

    effect = max(effects, key=_EFFECT_ORDER.__getitem__) if effects else "ALLOW"
    max_severity = (
        max((item.severity for item in findings), key=_SEVERITY_ORDER.__getitem__)
        if findings
        else "NONE"
    )
    return GuardianDecision(
        matches,
        tuple(findings),
        effect,
        effect in {"SUPPRESS", "PAUSE"},
        max_severity,
        unavailable,
    )


class GuardianService:
    RULE_VERSION = "guardian-v1"

    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def evaluate(self, state_evaluation_uid: str) -> GuardianEvaluation:
        existing = self._by_state_evaluation(state_evaluation_uid)
        if existing is not None:
            return existing
        with self._runtime.read_connection() as connection:
            state = connection.exec_driver_sql(
                "SELECT e.subject_uid,e.snapshot_uid,e.availability_state,e.evaluation_hash,"
                "e.created_at,q.data_health_status,q.fitness_status FROM state_evaluation e "
                "JOIN evaluation_snapshot s ON s.snapshot_uid=e.snapshot_uid "
                "JOIN quality_context q ON q.quality_context_uid=s.quality_context_uid "
                "WHERE e.evaluation_uid=?",
                (state_evaluation_uid,),
            ).one()
            facts = connection.exec_driver_sql(
                "SELECT f.fact_uid,f.fact_code,f.value_scaled,r.threshold_version_uid "
                "FROM state_evaluation_fact e JOIN fact_record f ON f.fact_uid=e.fact_uid "
                "JOIN rule_execution r ON r.rule_execution_uid=f.producer_rule_execution_uid "
                "WHERE e.evaluation_uid=?",
                (state_evaluation_uid,),
            ).all()
        by_code = {str(row.fact_code): row for row in facts}
        metric_values = {
            code: int(row.value_scaled)
            for code, row in by_code.items()
            if row.value_scaled is not None
        }
        fact_uids = {code: str(row.fact_uid) for code, row in by_code.items()}
        decision = guardian_decision(
            metric_values,
            None,
            availability_state=str(state.availability_state),
            data_health_status=str(state.data_health_status),
            fitness_status=str(state.fitness_status),
            fact_uids=fact_uids,
        )
        if not decision.unavailable:
            with self._runtime.read_connection() as threshold_connection:
                thresholds = resolve_thresholds_for_facts(
                    threshold_connection,
                    "GUARDIAN",
                    [by_code[rule[0]] for rule in _RULES],
                )
            decision = guardian_decision(
                metric_values,
                thresholds.entries,
                availability_state=str(state.availability_state),
                data_health_status=str(state.data_health_status),
                fitness_status=str(state.fitness_status),
                fact_uids=fact_uids,
            )
        findings = list(decision.risks)
        effect = decision.guardian_effect
        max_severity = decision.max_severity
        document = {
            "state_evaluation_hash": state.evaluation_hash,
            "rule_version": self.RULE_VERSION,
            "effect": effect,
            "risks": [
                (item.risk_tag, item.severity, item.reason_code, item.primary_fact_uid)
                for item in sorted(findings, key=lambda item: item.risk_tag)
            ],
        }
        digest = canonical_hash(document)
        uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO guardian_evaluation"
                "(guardian_uid,state_evaluation_uid,subject_uid,snapshot_uid,rule_version,"
                "guardian_effect,blocking,max_severity,evaluation_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    state_evaluation_uid,
                    state.subject_uid,
                    state.snapshot_uid,
                    self.RULE_VERSION,
                    effect,
                    int(effect in {"SUPPRESS", "PAUSE"}),
                    max_severity,
                    digest,
                    state.created_at,
                ),
            )
            for finding in findings:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO guardian_risk_tag"
                    "(guardian_uid,risk_tag,severity,reason_code,primary_fact_uid) "
                    "VALUES (?,?,?,?,?)",
                    (
                        uid,
                        finding.risk_tag,
                        finding.severity,
                        finding.reason_code,
                        finding.primary_fact_uid,
                    ),
                )
                if finding.primary_fact_uid is not None:
                    transaction.connection.exec_driver_sql(
                        "INSERT INTO guardian_risk_evidence"
                        "(guardian_uid,risk_tag,fact_uid,evidence_role) "
                        "VALUES (?,?,?,'SUPPORTING')",
                        (uid, finding.risk_tag, finding.primary_fact_uid),
                    )

        self._writer.submit(command).result()
        return self.get(uid)

    def get(self, guardian_uid: str) -> GuardianEvaluation:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT guardian_uid,state_evaluation_uid,guardian_effect,blocking,max_severity,"
                "evaluation_hash FROM guardian_evaluation WHERE guardian_uid=?",
                (guardian_uid,),
            ).one()
            risks = connection.exec_driver_sql(
                "SELECT risk_tag,severity,reason_code,primary_fact_uid FROM guardian_risk_tag "
                "WHERE guardian_uid=? ORDER BY risk_tag",
                (guardian_uid,),
            ).all()
        return GuardianEvaluation(
            str(row.guardian_uid),
            str(row.state_evaluation_uid),
            str(row.guardian_effect),
            bool(row.blocking),
            str(row.max_severity),
            str(row.evaluation_hash),
            tuple(
                RiskFinding(
                    str(item.risk_tag),
                    str(item.severity),
                    str(item.reason_code),
                    None if item.primary_fact_uid is None else str(item.primary_fact_uid),
                )
                for item in risks
            ),
        )

    def _by_state_evaluation(self, state_evaluation_uid: str) -> GuardianEvaluation | None:
        with self._runtime.read_connection() as connection:
            uid = connection.exec_driver_sql(
                "SELECT guardian_uid FROM guardian_evaluation WHERE state_evaluation_uid=?",
                (state_evaluation_uid,),
            ).scalar_one_or_none()
        return None if uid is None else self.get(str(uid))
