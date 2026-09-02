import json
from collections.abc import Mapping

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_analysis.models import Baseline, Fact
from market_monitor_analysis.thresholds import resolve_threshold_by_uid


class SnapshotNotSealedError(RuntimeError):
    pass


class UnknownFactRuleError(LookupError):
    pass


class UnknownFactCodeError(ValueError):
    pass


_GUARDIAN_FACT_CODES = {
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
}
_SCOUT_FACT_CODES = {
    "EARLY_ACTIVITY_PPM",
    "HEALTHY_BREADTH_PPM",
    "RELATIVE_STRENGTH_PPM",
    "TURNOVER_CONFIRMATION_PPM",
    "ETF_CONFIRMATION_PPM",
    "STYLE_SUPPORT_PPM",
    "LOW_CROWDING_PPM",
    "CONTINUITY_STRENGTHENING_PPM",
}


class FactExecutor:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer
        self._artifacts = ArtifactStore(runtime, writer)

    def execute(self, snapshot_uid: str, rule_key: str, rule_version: str) -> tuple[Fact, ...]:
        if rule_key != "OBJECTIVE_QUOTE_SUMMARY":
            raise UnknownFactRuleError(rule_key)
        with self._runtime.read_connection() as connection:
            snapshot = connection.exec_driver_sql(
                "SELECT s.snapshot_status,s.canonical_hash,s.quality_context_uid,s.as_of_time,"
                "m.artifact_sha256 FROM evaluation_snapshot s JOIN input_manifest m "
                "ON m.manifest_uid=s.manifest_uid "
                "WHERE s.snapshot_uid=?",
                (snapshot_uid,),
            ).one()
            if snapshot.snapshot_status != "SEALED":
                raise SnapshotNotSealedError("facts require a sealed snapshot")
            with self._artifacts.open_verified(str(snapshot.artifact_sha256)) as stream:
                document = json.load(stream)
            quote_uids = document["quote_uids"]
            placeholders = ",".join("?" for _ in quote_uids)
            rows = connection.exec_driver_sql(
                "SELECT q.quote_uid,q.price_status,q.price_scaled,q.price_scale,q.volume_status "
                f"FROM market_quote q WHERE q.quote_uid IN ({placeholders}) ORDER BY q.quote_uid",
                tuple(quote_uids),
            ).all()
        inputs = [tuple(row) for row in rows]
        input_hash = canonical_hash(inputs)
        total = len(rows)
        valid = sum(row.price_status == "VALUE" for row in rows)
        missing = total - valid
        values = (
            ("QUOTE_COUNT", total, 0, "COUNT"),
            ("VALID_PRICE_COUNT", valid, 0, "COUNT"),
            ("MISSING_PRICE_COUNT", missing, 0, "COUNT"),
            ("COVERAGE_PPM", 0 if total == 0 else valid * 1_000_000 // total, 0, "PPM"),
        )
        output_hash = canonical_hash(values)
        with self._runtime.read_connection() as connection:
            existing = connection.exec_driver_sql(
                "SELECT rule_execution_uid FROM rule_execution WHERE snapshot_uid=? "
                "AND rule_key=? AND rule_version=? AND input_hash=?",
                (snapshot_uid, rule_key, rule_version, input_hash),
            ).scalar_one_or_none()
        if existing is not None:
            return self._facts_for_execution(str(existing))
        execution_uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO rule_execution"
                "(rule_execution_uid,snapshot_uid,rule_key,rule_version,validation_status,"
                "input_hash,output_hash,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    execution_uid,
                    snapshot_uid,
                    rule_key,
                    rule_version,
                    "VALID",
                    input_hash,
                    output_hash,
                    snapshot.as_of_time,
                    snapshot.as_of_time,
                ),
            )
            for code, value, scale, unit in values:
                fact_uid = new_uid()
                digest = canonical_hash(
                    {
                        "snapshot_hash": snapshot.canonical_hash,
                        "rule_key": rule_key,
                        "rule_version": rule_version,
                        "fact_code": code,
                        "value_scaled": value,
                        "value_scale": scale,
                        "unit": unit,
                    }
                )
                transaction.connection.exec_driver_sql(
                    "INSERT INTO fact_record"
                    "(fact_uid,snapshot_uid,producer_rule_execution_uid,quality_context_uid,"
                    "fact_code,value_scaled,value_scale,value_status,unit,fact_hash) "
                    "VALUES (?,?,?,?,?,?,?,'VALUE',?,?)",
                    (
                        fact_uid,
                        snapshot_uid,
                        execution_uid,
                        snapshot.quality_context_uid,
                        code,
                        value,
                        scale,
                        unit,
                        digest,
                    ),
                )

        self._writer.submit(command).result()
        return self._facts_for_execution(execution_uid)

    def record_metrics(
        self,
        snapshot_uid: str,
        rule_key: str,
        rule_version: str,
        metrics: Mapping[str, int],
        *,
        threshold_version_uid: str,
        evidence_sha256: str | None = None,
    ) -> tuple[Fact, ...]:
        return self._record_metrics(
            snapshot_uid,
            rule_key,
            rule_version,
            metrics,
            _GUARDIAN_FACT_CODES,
            "GUARDIAN",
            threshold_version_uid,
            evidence_sha256,
        )

    def record_scout_metrics(
        self,
        snapshot_uid: str,
        rule_key: str,
        rule_version: str,
        metrics: Mapping[str, int],
        *,
        threshold_version_uid: str,
        evidence_sha256: str | None = None,
    ) -> tuple[Fact, ...]:
        return self._record_metrics(
            snapshot_uid,
            rule_key,
            rule_version,
            metrics,
            _SCOUT_FACT_CODES,
            "SCOUT",
            threshold_version_uid,
            evidence_sha256,
        )

    def _record_metrics(
        self,
        snapshot_uid: str,
        rule_key: str,
        rule_version: str,
        metrics: Mapping[str, int],
        allowed_codes: set[str],
        family: str,
        threshold_version_uid: str,
        evidence_sha256: str | None,
    ) -> tuple[Fact, ...]:
        if any(code not in allowed_codes for code in metrics):
            raise UnknownFactCodeError("metric code is not approved for this objective rule set")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000
            for value in metrics.values()
        ):
            raise ValueError("Guardian metrics must be integer PPM values from 0 to 1000000")
        with self._runtime.read_connection() as connection:
            snapshot = connection.exec_driver_sql(
                "SELECT snapshot_status,canonical_hash,quality_context_uid,as_of_time "
                "FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uid,),
            ).one()
        if snapshot.snapshot_status != "SEALED":
            raise SnapshotNotSealedError("facts require a sealed snapshot")
        with self._runtime.read_connection() as connection:
            thresholds = resolve_threshold_by_uid(connection, family, threshold_version_uid)
            if (
                evidence_sha256 is not None
                and connection.exec_driver_sql(
                    "SELECT 1 FROM artifact_object WHERE sha256=?", (evidence_sha256,)
                ).scalar_one_or_none()
                is None
            ):
                raise ValueError("metric evidence artifact is not registered")
        if evidence_sha256 is not None:
            with self._artifacts.open_verified(evidence_sha256):
                pass
        values = tuple(sorted(metrics.items()))
        digest = canonical_hash(
            {
                "snapshot_hash": snapshot.canonical_hash,
                "rule_key": rule_key,
                "producer_version": rule_version,
                "threshold_version_uid": thresholds.uid,
                "threshold_definition_hash": thresholds.definition_hash,
                "metrics": values,
                "evidence_sha256": evidence_sha256,
            }
        )
        output_hash = evidence_sha256 or digest
        with self._runtime.read_connection() as connection:
            existing = connection.exec_driver_sql(
                "SELECT rule_execution_uid FROM rule_execution WHERE snapshot_uid=? "
                "AND rule_key=? AND rule_version=? AND input_hash=?",
                (snapshot_uid, rule_key, rule_version, digest),
            ).scalar_one_or_none()
        if existing is not None:
            return self._facts_for_execution(str(existing))
        execution_uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO rule_execution"
                "(rule_execution_uid,snapshot_uid,rule_key,rule_version,threshold_version_uid,"
                "validation_status,input_hash,output_hash,started_at,finished_at) "
                "VALUES (?,?,?,?,?,'VALID',?,?,?,?)",
                (
                    execution_uid,
                    snapshot_uid,
                    rule_key,
                    rule_version,
                    thresholds.uid,
                    digest,
                    output_hash,
                    snapshot.as_of_time,
                    snapshot.as_of_time,
                ),
            )
            for code, value in values:
                fact_uid = new_uid()
                fact_hash = canonical_hash(
                    {
                        "snapshot_hash": snapshot.canonical_hash,
                        "rule_key": rule_key,
                        "producer_version": rule_version,
                        "threshold_version_uid": thresholds.uid,
                        "threshold_definition_hash": thresholds.definition_hash,
                        "evidence_sha256": evidence_sha256,
                        "fact_code": code,
                        "value_scaled": value,
                        "value_scale": 0,
                        "unit": "PPM",
                    }
                )
                transaction.connection.exec_driver_sql(
                    "INSERT INTO fact_record"
                    "(fact_uid,snapshot_uid,producer_rule_execution_uid,quality_context_uid,"
                    "fact_code,value_scaled,value_scale,value_status,unit,fact_hash) "
                    "VALUES (?,?,?,?,?,?,0,'VALUE','PPM',?)",
                    (
                        fact_uid,
                        snapshot_uid,
                        execution_uid,
                        snapshot.quality_context_uid,
                        code,
                        value,
                        fact_hash,
                    ),
                )

        self._writer.submit(command).result()
        return self._facts_for_execution(execution_uid)

    def record_baseline(
        self, subject_uid: str, fact_uid: str, period_kind: str, period_key: str
    ) -> Baseline:
        baseline_uid = new_uid()

        def command(transaction: TransactionContext) -> Baseline:
            fact = transaction.connection.exec_driver_sql(
                "SELECT fact_code,value_scaled,value_scale,value_status,snapshot_uid "
                "FROM fact_record WHERE fact_uid=?",
                (fact_uid,),
            ).one()
            latest = transaction.connection.exec_driver_sql(
                "SELECT max(version) FROM historical_baseline WHERE subject_uid=? "
                "AND fact_code=? AND period_kind=? AND period_key=?",
                (subject_uid, fact.fact_code, period_kind, period_key),
            ).scalar_one()
            version = 1 if latest is None else int(latest) + 1
            as_of = transaction.connection.exec_driver_sql(
                "SELECT as_of_time FROM evaluation_snapshot WHERE snapshot_uid=?",
                (fact.snapshot_uid,),
            ).scalar_one()
            transaction.connection.exec_driver_sql(
                "INSERT INTO historical_baseline"
                "(baseline_uid,subject_uid,fact_code,period_kind,period_key,version,value_scaled,"
                "value_scale,value_status,source_snapshot_uid,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    baseline_uid,
                    subject_uid,
                    fact.fact_code,
                    period_kind,
                    period_key,
                    version,
                    fact.value_scaled,
                    fact.value_scale,
                    fact.value_status,
                    fact.snapshot_uid,
                    as_of,
                ),
            )
            return Baseline(baseline_uid, version)

        return self._writer.submit(command).result()

    def _facts_for_execution(self, execution_uid: str) -> tuple[Fact, ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT fact_uid,fact_code,value_scaled,value_scale,unit,fact_hash "
                "FROM fact_record WHERE producer_rule_execution_uid=? ORDER BY fact_code",
                (execution_uid,),
            ).all()
        return tuple(
            Fact(
                str(row.fact_uid),
                str(row.fact_code),
                int(row.value_scaled),
                int(row.value_scale),
                str(row.unit),
                str(row.fact_hash),
            )
            for row in rows
        )
