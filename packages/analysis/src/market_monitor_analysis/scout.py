from collections.abc import Mapping
from dataclasses import dataclass

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_analysis.thresholds import resolve_thresholds_for_facts


@dataclass(frozen=True)
class OpportunityFinding:
    opportunity_tag: str
    reason_code: str
    primary_fact_uid: str


@dataclass(frozen=True)
class ScoutEvaluation:
    scout_uid: str
    guardian_uid: str
    guardian_effect: str
    scout_status: str
    scout_strength: str
    suppressed_by_guardian: bool
    evaluation_hash: str
    tags: tuple[OpportunityFinding, ...]


_RULES = (
    ("EARLY_ACTIVITY_PPM", "EARLY_ACTIVITY"),
    ("HEALTHY_BREADTH_PPM", "HEALTHY_BREADTH"),
    ("RELATIVE_STRENGTH_PPM", "RELATIVE_STRENGTH"),
    ("TURNOVER_CONFIRMATION_PPM", "TURNOVER_CONFIRMATION"),
    ("ETF_CONFIRMATION_PPM", "ETF_CONFIRMATION"),
    ("STYLE_SUPPORT_PPM", "STYLE_SUPPORT"),
    ("LOW_CROWDING_PPM", "LOW_CROWDING"),
    ("CONTINUITY_STRENGTHENING_PPM", "CONTINUITY_STRENGTHENING"),
)


def scout_threshold_matches(
    metrics: Mapping[str, int], thresholds: Mapping[str, int]
) -> tuple[str, ...]:
    """Return frozen Scout metric codes whose versioned threshold is met."""
    return tuple(
        fact_code
        for fact_code, _ in _RULES
        if fact_code in metrics and metrics[fact_code] >= thresholds[fact_code]
    )


def scout_rule_metadata(fact_code: str) -> str | None:
    """Expose the frozen opportunity tag for calibration evidence."""
    return next((tag for code, tag in _RULES if code == fact_code), None)


class ScoutService:
    RULE_VERSION = "scout-v1"

    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def evaluate(self, guardian_uid: str) -> ScoutEvaluation:
        existing = self._by_guardian(guardian_uid)
        if existing is not None:
            return existing
        with self._runtime.read_connection() as connection:
            guardian = connection.exec_driver_sql(
                "SELECT g.state_evaluation_uid,g.subject_uid,g.snapshot_uid,g.guardian_effect,"
                "g.evaluation_hash,e.created_at FROM guardian_evaluation g "
                "JOIN state_evaluation e ON e.evaluation_uid=g.state_evaluation_uid "
                "WHERE g.guardian_uid=?",
                (guardian_uid,),
            ).one()
            facts = connection.exec_driver_sql(
                "SELECT f.fact_uid,f.fact_code,f.value_scaled,r.threshold_version_uid "
                "FROM state_evaluation_fact e JOIN fact_record f ON f.fact_uid=e.fact_uid "
                "JOIN rule_execution r ON r.rule_execution_uid=f.producer_rule_execution_uid "
                "WHERE e.evaluation_uid=?",
                (guardian.state_evaluation_uid,),
            ).all()
            by_code = {str(row.fact_code): row for row in facts}
            comparison_facts = [by_code[code] for code, _ in _RULES if code in by_code]
            thresholds = (
                resolve_thresholds_for_facts(connection, "SCOUT", comparison_facts)
                if comparison_facts
                else None
            )
            findings = []
            for fact_code, tag in _RULES:
                fact = by_code.get(fact_code)
                if fact is not None and fact.value_scaled is not None:
                    if (
                        thresholds is not None
                        and int(fact.value_scaled) >= thresholds.entries[fact_code]
                    ):
                        findings.append(
                            OpportunityFinding(
                                tag, f"{tag}_OBJECTIVE_THRESHOLD_MET", str(fact.fact_uid)
                            )
                        )
        count = len(findings)
        status = "NONE" if count == 0 else "OBSERVING" if count < 3 else "ACTIVE"
        strength = "LOW" if count < 3 else "MEDIUM" if count < 6 else "HIGH"
        suppressed = guardian.guardian_effect in {"SUPPRESS", "PAUSE"}
        digest = canonical_hash(
            {
                "guardian_hash": guardian.evaluation_hash,
                "rule_version": self.RULE_VERSION,
                "status": status,
                "strength": strength,
                "suppressed": suppressed,
                "tags": [
                    (item.opportunity_tag, item.reason_code, item.primary_fact_uid)
                    for item in sorted(findings, key=lambda item: item.opportunity_tag)
                ],
            }
        )
        uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO scout_evaluation"
                "(scout_uid,guardian_uid,state_evaluation_uid,subject_uid,snapshot_uid,rule_version,"
                "scout_status,scout_strength,suppressed_by_guardian,guardian_effect,evaluation_hash,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    guardian_uid,
                    guardian.state_evaluation_uid,
                    guardian.subject_uid,
                    guardian.snapshot_uid,
                    self.RULE_VERSION,
                    status,
                    strength,
                    int(suppressed),
                    guardian.guardian_effect,
                    digest,
                    guardian.created_at,
                ),
            )
            for finding in findings:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO scout_opportunity_tag"
                    "(scout_uid,opportunity_tag,reason_code,primary_fact_uid) VALUES (?,?,?,?)",
                    (
                        uid,
                        finding.opportunity_tag,
                        finding.reason_code,
                        finding.primary_fact_uid,
                    ),
                )
                transaction.connection.exec_driver_sql(
                    "INSERT INTO scout_opportunity_evidence"
                    "(scout_uid,opportunity_tag,fact_uid,evidence_role) "
                    "VALUES (?,?,?,'SUPPORTING')",
                    (uid, finding.opportunity_tag, finding.primary_fact_uid),
                )

        self._writer.submit(command).result()
        return self.get(uid)

    def get(self, scout_uid: str) -> ScoutEvaluation:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT scout_uid,guardian_uid,guardian_effect,scout_status,scout_strength,"
                "suppressed_by_guardian,evaluation_hash FROM scout_evaluation WHERE scout_uid=?",
                (scout_uid,),
            ).one()
            tags = connection.exec_driver_sql(
                "SELECT opportunity_tag,reason_code,primary_fact_uid FROM scout_opportunity_tag "
                "WHERE scout_uid=? ORDER BY opportunity_tag",
                (scout_uid,),
            ).all()
        return ScoutEvaluation(
            str(row.scout_uid),
            str(row.guardian_uid),
            str(row.guardian_effect),
            str(row.scout_status),
            str(row.scout_strength),
            bool(row.suppressed_by_guardian),
            str(row.evaluation_hash),
            tuple(
                OpportunityFinding(
                    str(item.opportunity_tag), str(item.reason_code), str(item.primary_fact_uid)
                )
                for item in tags
            ),
        )

    def _by_guardian(self, guardian_uid: str) -> ScoutEvaluation | None:
        with self._runtime.read_connection() as connection:
            uid = connection.exec_driver_sql(
                "SELECT scout_uid FROM scout_evaluation WHERE guardian_uid=?", (guardian_uid,)
            ).scalar_one_or_none()
        return None if uid is None else self.get(str(uid))
