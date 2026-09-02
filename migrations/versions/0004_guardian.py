"""Create M4 Guardian tables.

Revision ID: 0004_guardian
Revises: 0003_snapshot_facts_state
"""

# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0004_guardian"
down_revision: str | None = "0003_snapshot_facts_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE guardian_evaluation (
            guardian_uid TEXT PRIMARY KEY,
            state_evaluation_uid TEXT NOT NULL UNIQUE
                REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            rule_version TEXT NOT NULL,
            guardian_effect TEXT NOT NULL
                CHECK(guardian_effect IN ('ALLOW','ALLOW_WITH_WARNING','DOWNGRADE','SUPPRESS','PAUSE')),
            blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
            max_severity TEXT NOT NULL
                CHECK(max_severity IN ('NONE','INFO','WARNING','HIGH','CRITICAL')),
            evaluation_hash TEXT NOT NULL UNIQUE CHECK(length(evaluation_hash)=64),
            created_at TEXT NOT NULL,
            CHECK((guardian_effect IN ('SUPPRESS','PAUSE'))=blocking)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE guardian_risk_tag (
            guardian_uid TEXT NOT NULL REFERENCES guardian_evaluation(guardian_uid) ON DELETE RESTRICT,
            risk_tag TEXT NOT NULL CHECK(risk_tag IN
                ('RISING_TOO_FAST','HEAD_CONCENTRATION_HIGH','INTERNAL_DIVERGENCE',
                 'CROWDING_INCREASING','LIQUIDITY_WEAKENING','CORE_MEMBERS_WEAKENING',
                 'BREADTH_COLLAPSING','STAMPEDE_RISK','T1_CHASING_RISK','DATA_LIMITATION',
                 'EARLY_SIGNAL_FAILED')),
            severity TEXT NOT NULL CHECK(severity IN ('INFO','WARNING','HIGH','CRITICAL')),
            reason_code TEXT NOT NULL,
            primary_fact_uid TEXT REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            PRIMARY KEY(guardian_uid,risk_tag)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE guardian_risk_evidence (
            guardian_uid TEXT NOT NULL,
            risk_tag TEXT NOT NULL,
            fact_uid TEXT NOT NULL REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            evidence_role TEXT NOT NULL CHECK(evidence_role IN ('SUPPORTING','CONTRARY')),
            PRIMARY KEY(guardian_uid,risk_tag,fact_uid,evidence_role),
            FOREIGN KEY(guardian_uid,risk_tag)
                REFERENCES guardian_risk_tag(guardian_uid,risk_tag) ON DELETE RESTRICT
        ) STRICT
        """
    )
    op.execute("CREATE INDEX guardian_subject_idx ON guardian_evaluation(subject_uid,created_at)")
    for table in ("guardian_evaluation", "guardian_risk_tag", "guardian_risk_evidence"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END
            """
        )
    op.execute(
        """
        INSERT INTO enum_registry(enum_name,enum_value,ordinal,source_version,active) VALUES
        ('RiskTag','RISING_TOO_FAST',0,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','HEAD_CONCENTRATION_HIGH',1,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','INTERNAL_DIVERGENCE',2,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','CROWDING_INCREASING',3,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','LIQUIDITY_WEAKENING',4,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','CORE_MEMBERS_WEAKENING',5,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','BREADTH_COLLAPSING',6,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','STAMPEDE_RISK',7,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','T1_CHASING_RISK',8,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','DATA_LIMITATION',9,'Consolidated Architecture Baseline v1.0',1),
        ('RiskTag','EARLY_SIGNAL_FAILED',10,'Consolidated Architecture Baseline v1.0',1),
        ('GuardianEffect','ALLOW',0,'Consolidated Architecture Baseline v1.0',1),
        ('GuardianEffect','ALLOW_WITH_WARNING',1,'Consolidated Architecture Baseline v1.0',1),
        ('GuardianEffect','DOWNGRADE',2,'Consolidated Architecture Baseline v1.0',1),
        ('GuardianEffect','SUPPRESS',3,'Consolidated Architecture Baseline v1.0',1),
        ('GuardianEffect','PAUSE',4,'Consolidated Architecture Baseline v1.0',1)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE guardian_risk_evidence")
    op.execute("DROP TABLE guardian_risk_tag")
    op.execute("DROP TABLE guardian_evaluation")
    op.execute("DELETE FROM enum_registry WHERE enum_name IN ('RiskTag','GuardianEffect')")
