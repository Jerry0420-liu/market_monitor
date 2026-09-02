"""Create M3 snapshot, fact, and state tables.

Revision ID: 0003_snapshot_facts_state
Revises: 0002_reference_and_acquisition
"""

# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0003_snapshot_facts_state"
down_revision: str | None = "0002_reference_and_acquisition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    statements = (
        """
        CREATE TABLE reference_version_bundle (
            bundle_uid TEXT PRIMARY KEY,
            canonical_hash TEXT NOT NULL UNIQUE CHECK(length(canonical_hash)=64 AND canonical_hash NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE reference_version_entry (
            bundle_uid TEXT NOT NULL REFERENCES reference_version_bundle(bundle_uid) ON DELETE RESTRICT,
            entity_kind TEXT NOT NULL CHECK(entity_kind IN ('INSTRUMENT','SECTOR','MEMBERSHIP','MAPPING','CALENDAR')),
            entity_uid TEXT NOT NULL,
            version_uid TEXT NOT NULL,
            PRIMARY KEY(bundle_uid,entity_kind,entity_uid)
        ) STRICT
        """,
        """
        CREATE TABLE input_manifest (
            manifest_uid TEXT PRIMARY KEY,
            canonical_hash TEXT NOT NULL UNIQUE CHECK(length(canonical_hash)=64 AND canonical_hash NOT GLOB '*[^0-9a-f]*'),
            artifact_sha256 TEXT NOT NULL REFERENCES artifact_object(sha256) ON DELETE RESTRICT,
            as_of_time TEXT NOT NULL,
            created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE quality_context (
            quality_context_uid TEXT PRIMARY KEY,
            data_health_status TEXT NOT NULL CHECK(data_health_status IN ('HEALTHY','DEGRADED','UNHEALTHY','UNKNOWN')),
            fitness_status TEXT NOT NULL CHECK(fitness_status IN ('FIT','FIT_WITH_LIMITATIONS','UNFIT','UNKNOWN')),
            evidence_sufficiency TEXT NOT NULL CHECK(evidence_sufficiency IN ('LOW','MEDIUM','HIGH','UNKNOWN')),
            coverage_ppm INTEGER NOT NULL CHECK(coverage_ppm BETWEEN 0 AND 1000000),
            max_age_ms INTEGER NOT NULL CHECK(max_age_ms >= 0),
            observed_at TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            CHECK(valid_until >= observed_at),
            CHECK(data_health_status!='UNHEALTHY' OR fitness_status IN ('UNFIT','UNKNOWN'))
        ) STRICT
        """,
        """
        CREATE TABLE quality_limitation (
            limitation_uid TEXT PRIMARY KEY,
            quality_context_uid TEXT NOT NULL REFERENCES quality_context(quality_context_uid) ON DELETE RESTRICT,
            limitation_code TEXT NOT NULL,
            detail TEXT NOT NULL,
            UNIQUE(quality_context_uid,limitation_code)
        ) STRICT
        """,
        """
        CREATE TABLE evaluation_snapshot (
            snapshot_uid TEXT PRIMARY KEY,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            manifest_uid TEXT NOT NULL REFERENCES input_manifest(manifest_uid) ON DELETE RESTRICT,
            bundle_uid TEXT NOT NULL REFERENCES reference_version_bundle(bundle_uid) ON DELETE RESTRICT,
            quality_context_uid TEXT NOT NULL REFERENCES quality_context(quality_context_uid) ON DELETE RESTRICT,
            as_of_time TEXT NOT NULL,
            max_skew_ms INTEGER NOT NULL CHECK(max_skew_ms >= 0),
            evaluation_disposition TEXT NOT NULL CHECK(evaluation_disposition IN ('OFFICIAL','STALE_AUDIT','USER_QUERY','SHADOW','HISTORICAL_REPLAY','CORRECTED_RESEARCH')),
            snapshot_status TEXT NOT NULL CHECK(snapshot_status IN ('DRAFT','SEALED','REJECTED')),
            canonical_hash TEXT NOT NULL UNIQUE CHECK(length(canonical_hash)=64 AND canonical_hash NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            sealed_at TEXT,
            CHECK((snapshot_status='SEALED')=(sealed_at IS NOT NULL))
        ) STRICT
        """,
        "CREATE INDEX evaluation_snapshot_subject_time_idx ON evaluation_snapshot(subject_uid,as_of_time)",
        """
        CREATE TABLE capability_snapshot (
            snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            capability TEXT NOT NULL,
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            health_report_uid TEXT REFERENCES capability_health_report(report_uid) ON DELETE RESTRICT,
            health_status TEXT NOT NULL CHECK(health_status IN ('HEALTHY','DEGRADED','UNHEALTHY','UNKNOWN')),
            fitness_status TEXT NOT NULL CHECK(fitness_status IN ('FIT','FIT_WITH_LIMITATIONS','UNFIT','UNKNOWN')),
            coverage_ppm INTEGER NOT NULL CHECK(coverage_ppm BETWEEN 0 AND 1000000),
            latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
            required INTEGER NOT NULL CHECK(required IN (0,1)),
            PRIMARY KEY(snapshot_uid,capability)
        ) STRICT
        """,
        """
        CREATE TABLE rule_execution (
            rule_execution_uid TEXT PRIMARY KEY,
            snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            rule_key TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            validation_status TEXT NOT NULL CHECK(validation_status IN ('VALID','INVALID','UNFIT')),
            input_hash TEXT NOT NULL CHECK(length(input_hash)=64),
            output_hash TEXT NOT NULL CHECK(length(output_hash)=64),
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            UNIQUE(snapshot_uid,rule_key,rule_version,input_hash)
        ) STRICT
        """,
        """
        CREATE TABLE fact_record (
            fact_uid TEXT PRIMARY KEY,
            snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            producer_rule_execution_uid TEXT NOT NULL REFERENCES rule_execution(rule_execution_uid) ON DELETE RESTRICT,
            quality_context_uid TEXT NOT NULL REFERENCES quality_context(quality_context_uid) ON DELETE RESTRICT,
            fact_code TEXT NOT NULL,
            value_scaled INTEGER,
            value_scale INTEGER NOT NULL CHECK(value_scale >= 0),
            value_status TEXT NOT NULL CHECK(value_status IN ('VALUE','MISSING','NOT_APPLICABLE','STALE','INVALID')),
            unit TEXT NOT NULL,
            fact_hash TEXT NOT NULL UNIQUE CHECK(length(fact_hash)=64),
            CHECK((value_status='VALUE')=(value_scaled IS NOT NULL)),
            UNIQUE(snapshot_uid,producer_rule_execution_uid,fact_code)
        ) STRICT
        """,
        "CREATE INDEX fact_record_snapshot_code_idx ON fact_record(snapshot_uid,fact_code)",
        """
        CREATE TABLE fact_specific_evidence (
            fact_uid TEXT NOT NULL REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            quote_uid TEXT NOT NULL REFERENCES market_quote(quote_uid) ON DELETE RESTRICT,
            evidence_role TEXT NOT NULL CHECK(evidence_role IN ('SUPPORTING','CONTRARY','KEY_INPUT')),
            PRIMARY KEY(fact_uid,quote_uid,evidence_role)
        ) STRICT
        """,
        """
        CREATE TABLE historical_baseline (
            baseline_uid TEXT PRIMARY KEY,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            fact_code TEXT NOT NULL,
            period_kind TEXT NOT NULL CHECK(period_kind IN ('DAILY','PHASE')),
            period_key TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            value_scaled INTEGER,
            value_scale INTEGER NOT NULL CHECK(value_scale >= 0),
            value_status TEXT NOT NULL CHECK(value_status IN ('VALUE','MISSING','NOT_APPLICABLE','STALE','INVALID')),
            source_snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            CHECK((value_status='VALUE')=(value_scaled IS NOT NULL)),
            UNIQUE(subject_uid,fact_code,period_kind,period_key,version)
        ) STRICT
        """,
        """
        CREATE TABLE state_evaluation (
            evaluation_uid TEXT PRIMARY KEY,
            snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            evaluation_disposition TEXT NOT NULL CHECK(evaluation_disposition IN ('OFFICIAL','STALE_AUDIT','USER_QUERY','SHADOW','HISTORICAL_REPLAY','CORRECTED_RESEARCH')),
            availability_state TEXT NOT NULL CHECK(availability_state IN ('AVAILABLE','WARMING_UP','SUSPENDED','UNAVAILABLE')),
            lifecycle_state TEXT CHECK(lifecycle_state IN ('OBSERVING','STARTING','EXPANDING','ACCELERATING','DIVERGING','DECLINING')),
            evaluation_hash TEXT NOT NULL UNIQUE CHECK(length(evaluation_hash)=64),
            created_at TEXT NOT NULL,
            CHECK((availability_state='AVAILABLE')=(lifecycle_state IS NOT NULL))
        ) STRICT
        """,
        """
        CREATE TABLE state_evaluation_fact (
            evaluation_uid TEXT NOT NULL REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            fact_uid TEXT NOT NULL REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            PRIMARY KEY(evaluation_uid,fact_uid)
        ) STRICT
        """,
        """
        CREATE TABLE transition_candidate (
            subject_uid TEXT PRIMARY KEY REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            target_lifecycle_state TEXT NOT NULL CHECK(target_lifecycle_state IN ('OBSERVING','STARTING','EXPANDING','ACCELERATING','DIVERGING','DECLINING')),
            confirmation_count INTEGER NOT NULL CHECK(confirmation_count > 0),
            first_evaluation_uid TEXT NOT NULL REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            latest_evaluation_uid TEXT NOT NULL REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK(version > 0)
        ) STRICT
        """,
        """
        CREATE TABLE state_transition (
            transition_uid TEXT PRIMARY KEY,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            from_lifecycle_state TEXT CHECK(from_lifecycle_state IN ('OBSERVING','STARTING','EXPANDING','ACCELERATING','DIVERGING','DECLINING')),
            to_lifecycle_state TEXT NOT NULL CHECK(to_lifecycle_state IN ('OBSERVING','STARTING','EXPANDING','ACCELERATING','DIVERGING','DECLINING')),
            evaluation_uid TEXT NOT NULL UNIQUE REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            occurred_at TEXT NOT NULL,
            CHECK(from_lifecycle_state IS NULL OR from_lifecycle_state != to_lifecycle_state)
        ) STRICT
        """,
        """
        CREATE TABLE current_state_projection (
            subject_uid TEXT PRIMARY KEY REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            availability_state TEXT NOT NULL CHECK(availability_state IN ('AVAILABLE','WARMING_UP','SUSPENDED','UNAVAILABLE')),
            effective_lifecycle_state TEXT CHECK(effective_lifecycle_state IN ('OBSERVING','STARTING','EXPANDING','ACCELERATING','DIVERGING','DECLINING')),
            last_valid_lifecycle_state TEXT CHECK(last_valid_lifecycle_state IN ('OBSERVING','STARTING','EXPANDING','ACCELERATING','DIVERGING','DECLINING')),
            last_valid_as_of_time TEXT,
            evaluation_uid TEXT NOT NULL REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            as_of_time TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            rewarm_required INTEGER NOT NULL CHECK(rewarm_required IN (0,1)),
            CHECK((availability_state='AVAILABLE')=(effective_lifecycle_state IS NOT NULL)),
            CHECK((last_valid_lifecycle_state IS NULL)=(last_valid_as_of_time IS NULL))
        ) STRICT
        """,
        """
        CREATE TRIGGER evaluation_snapshot_sealed_immutable
        BEFORE UPDATE ON evaluation_snapshot WHEN OLD.snapshot_status='SEALED'
        BEGIN SELECT RAISE(ABORT,'sealed evaluation_snapshot is immutable'); END
        """,
        """
        CREATE TRIGGER evaluation_snapshot_delete_immutable
        BEFORE DELETE ON evaluation_snapshot
        BEGIN SELECT RAISE(ABORT,'evaluation_snapshot is immutable'); END
        """,
        """
        CREATE TRIGGER input_manifest_immutable BEFORE UPDATE ON input_manifest
        BEGIN SELECT RAISE(ABORT,'input_manifest is immutable'); END
        """,
        """
        CREATE TRIGGER fact_record_immutable BEFORE UPDATE ON fact_record
        BEGIN SELECT RAISE(ABORT,'fact_record is immutable'); END
        """,
        """
        CREATE TRIGGER rule_execution_immutable BEFORE UPDATE ON rule_execution
        BEGIN SELECT RAISE(ABORT,'rule_execution is immutable'); END
        """,
        """
        CREATE TRIGGER state_evaluation_immutable BEFORE UPDATE ON state_evaluation
        BEGIN SELECT RAISE(ABORT,'state_evaluation is immutable'); END
        """,
        """
        CREATE TRIGGER state_evaluation_fact_immutable_update BEFORE UPDATE ON state_evaluation_fact
        BEGIN SELECT RAISE(ABORT,'state_evaluation_fact is immutable'); END
        """,
        """
        CREATE TRIGGER state_evaluation_fact_immutable_delete BEFORE DELETE ON state_evaluation_fact
        BEGIN SELECT RAISE(ABORT,'state_evaluation_fact is immutable'); END
        """,
    )
    for statement in statements:
        op.execute(statement)
    op.execute(
        """
        INSERT INTO enum_registry(enum_name,enum_value,ordinal,source_version,active) VALUES
        ('EvaluationDisposition','OFFICIAL',0,'Consolidated Architecture Baseline v1.0',1),
        ('EvaluationDisposition','STALE_AUDIT',1,'Consolidated Architecture Baseline v1.0',1),
        ('EvaluationDisposition','USER_QUERY',2,'Consolidated Architecture Baseline v1.0',1),
        ('EvaluationDisposition','SHADOW',3,'Consolidated Architecture Baseline v1.0',1),
        ('EvaluationDisposition','HISTORICAL_REPLAY',4,'Consolidated Architecture Baseline v1.0',1),
        ('EvaluationDisposition','CORRECTED_RESEARCH',5,'Consolidated Architecture Baseline v1.0',1),
        ('SnapshotStatus','DRAFT',0,'Consolidated Architecture Baseline v1.0',1),
        ('SnapshotStatus','SEALED',1,'Consolidated Architecture Baseline v1.0',1),
        ('SnapshotStatus','REJECTED',2,'Consolidated Architecture Baseline v1.0',1),
        ('AvailabilityState','AVAILABLE',0,'Consolidated Architecture Baseline v1.0',1),
        ('AvailabilityState','WARMING_UP',1,'Consolidated Architecture Baseline v1.0',1),
        ('AvailabilityState','SUSPENDED',2,'Consolidated Architecture Baseline v1.0',1),
        ('AvailabilityState','UNAVAILABLE',3,'Consolidated Architecture Baseline v1.0',1),
        ('LifecycleState','OBSERVING',0,'Consolidated Architecture Baseline v1.0',1),
        ('LifecycleState','STARTING',1,'Consolidated Architecture Baseline v1.0',1),
        ('LifecycleState','EXPANDING',2,'Consolidated Architecture Baseline v1.0',1),
        ('LifecycleState','ACCELERATING',3,'Consolidated Architecture Baseline v1.0',1),
        ('LifecycleState','DIVERGING',4,'Consolidated Architecture Baseline v1.0',1),
        ('LifecycleState','DECLINING',5,'Consolidated Architecture Baseline v1.0',1),
        ('EvidenceSufficiency','LOW',0,'Consolidated Architecture Baseline v1.0',1),
        ('EvidenceSufficiency','MEDIUM',1,'Consolidated Architecture Baseline v1.0',1),
        ('EvidenceSufficiency','HIGH',2,'Consolidated Architecture Baseline v1.0',1),
        ('EvidenceSufficiency','UNKNOWN',3,'Consolidated Architecture Baseline v1.0',1),
        ('RuleValidationStatus','VALID',0,'Consolidated Architecture Baseline v1.0',1),
        ('RuleValidationStatus','INVALID',1,'Consolidated Architecture Baseline v1.0',1),
        ('RuleValidationStatus','UNFIT',2,'Consolidated Architecture Baseline v1.0',1)
        """
    )


def downgrade() -> None:
    for name in (
        "current_state_projection",
        "state_transition",
        "transition_candidate",
        "state_evaluation_fact",
        "state_evaluation",
        "historical_baseline",
        "fact_specific_evidence",
        "fact_record",
        "rule_execution",
        "capability_snapshot",
        "evaluation_snapshot",
        "quality_limitation",
        "quality_context",
        "input_manifest",
        "reference_version_entry",
        "reference_version_bundle",
    ):
        op.execute(f"DROP TABLE {name}")
    op.execute(
        "DELETE FROM enum_registry WHERE enum_name IN "
        "('EvaluationDisposition','SnapshotStatus','AvailabilityState','LifecycleState',"
        "'EvidenceSufficiency','RuleValidationStatus')"
    )
