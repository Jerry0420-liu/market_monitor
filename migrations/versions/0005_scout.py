"""Create M5 Scout tables.

Revision ID: 0005_scout
Revises: 0004_guardian
"""

# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0005_scout"
down_revision: str | None = "0004_guardian"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scout_evaluation (
            scout_uid TEXT PRIMARY KEY,
            guardian_uid TEXT NOT NULL UNIQUE REFERENCES guardian_evaluation(guardian_uid) ON DELETE RESTRICT,
            state_evaluation_uid TEXT NOT NULL REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            rule_version TEXT NOT NULL,
            scout_status TEXT NOT NULL CHECK(scout_status IN ('NONE','OBSERVING','ACTIVE')),
            scout_strength TEXT NOT NULL CHECK(scout_strength IN ('LOW','MEDIUM','HIGH')),
            suppressed_by_guardian INTEGER NOT NULL CHECK(suppressed_by_guardian IN (0,1)),
            guardian_effect TEXT NOT NULL CHECK(guardian_effect IN ('ALLOW','ALLOW_WITH_WARNING','DOWNGRADE','SUPPRESS','PAUSE')),
            evaluation_hash TEXT NOT NULL UNIQUE CHECK(length(evaluation_hash)=64),
            created_at TEXT NOT NULL,
            CHECK((guardian_effect IN ('SUPPRESS','PAUSE'))=suppressed_by_guardian),
            CHECK(scout_status!='NONE' OR scout_strength='LOW')
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE scout_opportunity_tag (
            scout_uid TEXT NOT NULL REFERENCES scout_evaluation(scout_uid) ON DELETE RESTRICT,
            opportunity_tag TEXT NOT NULL CHECK(opportunity_tag IN
                ('EARLY_ACTIVITY','HEALTHY_BREADTH','RELATIVE_STRENGTH','TURNOVER_CONFIRMATION',
                 'ETF_CONFIRMATION','STYLE_SUPPORT','LOW_CROWDING','CONTINUITY_STRENGTHENING')),
            reason_code TEXT NOT NULL,
            primary_fact_uid TEXT NOT NULL REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            PRIMARY KEY(scout_uid,opportunity_tag)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE scout_opportunity_evidence (
            scout_uid TEXT NOT NULL,
            opportunity_tag TEXT NOT NULL,
            fact_uid TEXT NOT NULL REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            evidence_role TEXT NOT NULL CHECK(evidence_role IN ('SUPPORTING','CONTRARY')),
            PRIMARY KEY(scout_uid,opportunity_tag,fact_uid,evidence_role),
            FOREIGN KEY(scout_uid,opportunity_tag)
                REFERENCES scout_opportunity_tag(scout_uid,opportunity_tag) ON DELETE RESTRICT
        ) STRICT
        """
    )
    op.execute("CREATE INDEX scout_subject_idx ON scout_evaluation(subject_uid,created_at)")
    for table in ("scout_evaluation", "scout_opportunity_tag", "scout_opportunity_evidence"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END"
        )
    op.execute(
        """
        INSERT INTO enum_registry(enum_name,enum_value,ordinal,source_version,active) VALUES
        ('OpportunityTag','EARLY_ACTIVITY',0,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','HEALTHY_BREADTH',1,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','RELATIVE_STRENGTH',2,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','TURNOVER_CONFIRMATION',3,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','ETF_CONFIRMATION',4,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','STYLE_SUPPORT',5,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','LOW_CROWDING',6,'Consolidated Architecture Baseline v1.0',1),
        ('OpportunityTag','CONTINUITY_STRENGTHENING',7,'Consolidated Architecture Baseline v1.0',1),
        ('ScoutStatus','NONE',0,'API Contract v1.2',1),
        ('ScoutStatus','OBSERVING',1,'API Contract v1.2',1),
        ('ScoutStatus','ACTIVE',2,'API Contract v1.2',1),
        ('ScoutStrength','LOW',0,'API Contract v1.2',1),
        ('ScoutStrength','MEDIUM',1,'API Contract v1.2',1),
        ('ScoutStrength','HIGH',2,'API Contract v1.2',1)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE scout_opportunity_evidence")
    op.execute("DROP TABLE scout_opportunity_tag")
    op.execute("DROP TABLE scout_evaluation")
    op.execute(
        "DELETE FROM enum_registry WHERE enum_name IN ('OpportunityTag','ScoutStatus','ScoutStrength')"
    )
