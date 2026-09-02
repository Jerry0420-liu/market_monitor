"""Create M6 event, analysis commit, and notification tables.

Revision ID: 0006_events_notifications
Revises: 0005_scout
"""

# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0006_events_notifications"
down_revision: str | None = "0005_scout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis_commit (
            commit_uid TEXT PRIMARY KEY,
            snapshot_uid TEXT NOT NULL UNIQUE REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            state_evaluation_uid TEXT NOT NULL UNIQUE REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            guardian_uid TEXT NOT NULL UNIQUE REFERENCES guardian_evaluation(guardian_uid) ON DELETE RESTRICT,
            scout_uid TEXT NOT NULL UNIQUE REFERENCES scout_evaluation(scout_uid) ON DELETE RESTRICT,
            evaluation_disposition TEXT NOT NULL CHECK(evaluation_disposition='OFFICIAL'),
            commit_hash TEXT NOT NULL UNIQUE CHECK(length(commit_hash)=64),
            committed_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE market_event (
            event_uid TEXT PRIMARY KEY,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            event_kind TEXT NOT NULL CHECK(length(event_kind)>0),
            related_key TEXT NOT NULL CHECK(length(related_key)=64),
            created_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute("CREATE INDEX market_event_subject_idx ON market_event(subject_uid,created_at)")
    op.execute("CREATE INDEX market_event_related_idx ON market_event(related_key,created_at)")
    op.execute(
        """
        CREATE TABLE event_version (
            event_version_uid TEXT PRIMARY KEY,
            event_uid TEXT NOT NULL REFERENCES market_event(event_uid) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK(version>0),
            event_status TEXT NOT NULL CHECK(event_status IN ('CANDIDATE','ACTIVE','RESOLVED','INVALIDATED')),
            change_type TEXT NOT NULL CHECK(change_type IN ('CREATED','CHANGED','RESOLVED','INVALIDATED')),
            analysis_commit_uid TEXT NOT NULL REFERENCES analysis_commit(commit_uid) ON DELETE RESTRICT,
            state_evaluation_uid TEXT NOT NULL REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            guardian_uid TEXT NOT NULL REFERENCES guardian_evaluation(guardian_uid) ON DELETE RESTRICT,
            scout_uid TEXT NOT NULL REFERENCES scout_evaluation(scout_uid) ON DELETE RESTRICT,
            version_hash TEXT NOT NULL UNIQUE CHECK(length(version_hash)=64),
            created_at TEXT NOT NULL,
            UNIQUE(event_uid,version)
        ) STRICT
        """
    )
    op.execute("CREATE INDEX event_version_event_idx ON event_version(event_uid,version)")
    op.execute(
        """
        CREATE TABLE event_evidence (
            event_version_uid TEXT NOT NULL REFERENCES event_version(event_version_uid) ON DELETE RESTRICT,
            fact_uid TEXT NOT NULL REFERENCES fact_record(fact_uid) ON DELETE RESTRICT,
            evidence_role TEXT NOT NULL CHECK(evidence_role IN ('SUPPORTING','CONTRARY')),
            PRIMARY KEY(event_version_uid,fact_uid,evidence_role)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE current_event_projection (
            related_key TEXT PRIMARY KEY CHECK(length(related_key)=64),
            event_uid TEXT NOT NULL REFERENCES market_event(event_uid) ON DELETE RESTRICT,
            event_version_uid TEXT NOT NULL UNIQUE REFERENCES event_version(event_version_uid) ON DELETE RESTRICT,
            event_status TEXT NOT NULL CHECK(event_status IN ('CANDIDATE','ACTIVE','RESOLVED','INVALIDATED')),
            version INTEGER NOT NULL CHECK(version>0),
            updated_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX active_event_related_key_unique "
        "ON current_event_projection(related_key) WHERE event_status IN ('CANDIDATE','ACTIVE')"
    )
    op.execute(
        """
        CREATE TABLE notification_intent (
            intent_uid TEXT PRIMARY KEY,
            event_version_uid TEXT NOT NULL REFERENCES event_version(event_version_uid) ON DELETE RESTRICT,
            channel TEXT NOT NULL CHECK(channel IN ('IN_APP','WEBHOOK')),
            intent_kind TEXT NOT NULL CHECK(intent_kind IN ('RISK','ATTENTION','RESOLUTION')),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
            frozen_context_json TEXT NOT NULL CHECK(length(frozen_context_json)>0),
            frozen_context_hash TEXT NOT NULL CHECK(length(frozen_context_hash)=64),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            restore_generation INTEGER NOT NULL CHECK(restore_generation>=0),
            UNIQUE(event_version_uid,channel)
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX notification_generation_idx "
        "ON notification_intent(restore_generation,expires_at)"
    )
    op.execute(
        """
        CREATE TABLE notification_delivery_state (
            intent_uid TEXT PRIMARY KEY REFERENCES notification_intent(intent_uid) ON DELETE RESTRICT,
            delivery_status TEXT NOT NULL CHECK(delivery_status IN
                ('PENDING','PROCESSING','RETRY_WAIT','DELIVERED','FAILED','CANCELLED','EXPIRED')),
            attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
            next_attempt_at TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            last_error_code TEXT,
            version INTEGER NOT NULL CHECK(version>0),
            updated_at TEXT NOT NULL,
            CHECK((delivery_status='PROCESSING')=(lease_owner IS NOT NULL AND lease_until IS NOT NULL))
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX delivery_claim_idx ON notification_delivery_state"
        "(delivery_status,next_attempt_at,lease_until)"
    )
    op.execute(
        """
        CREATE TABLE delivery_attempt (
            attempt_uid TEXT PRIMARY KEY,
            intent_uid TEXT NOT NULL REFERENCES notification_intent(intent_uid) ON DELETE RESTRICT,
            attempt_number INTEGER NOT NULL CHECK(attempt_number>0),
            outcome TEXT NOT NULL CHECK(outcome IN
                ('DELIVERED','RETRYABLE_FAILURE','PERMANENT_FAILURE')),
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            provider_reference TEXT,
            error_code TEXT,
            UNIQUE(intent_uid,attempt_number)
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX delivery_attempt_intent_idx ON delivery_attempt(intent_uid,attempt_number)"
    )
    op.execute(
        """
        CREATE TABLE audit_record (
            audit_uid TEXT PRIMARY KEY,
            action TEXT NOT NULL CHECK(length(action)>0),
            subject_uid TEXT,
            analysis_commit_uid TEXT REFERENCES analysis_commit(commit_uid) ON DELETE RESTRICT,
            detail_hash TEXT NOT NULL CHECK(length(detail_hash)=64),
            created_at TEXT NOT NULL
        ) STRICT
        """
    )
    for table in (
        "analysis_commit",
        "audit_record",
        "market_event",
        "event_version",
        "event_evidence",
        "notification_intent",
        "delivery_attempt",
    ):
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
        ('EventStatus','CANDIDATE',0,'Consolidated Architecture Baseline v1.0',1),
        ('EventStatus','ACTIVE',1,'Consolidated Architecture Baseline v1.0',1),
        ('EventStatus','RESOLVED',2,'Consolidated Architecture Baseline v1.0',1),
        ('EventStatus','INVALIDATED',3,'Consolidated Architecture Baseline v1.0',1),
        ('DeliveryStatus','PENDING',0,'Codex Brief v1.0',1),
        ('DeliveryStatus','PROCESSING',1,'Codex Brief v1.0',1),
        ('DeliveryStatus','RETRY_WAIT',2,'Codex Brief v1.0',1),
        ('DeliveryStatus','DELIVERED',3,'Codex Brief v1.0',1),
        ('DeliveryStatus','FAILED',4,'Codex Brief v1.0',1),
        ('DeliveryStatus','CANCELLED',5,'Codex Brief v1.0',1),
        ('DeliveryStatus','EXPIRED',6,'Codex Brief v1.0',1)
        """
    )
    op.execute(
        "INSERT INTO system_metadata(key,value,updated_at,version) "
        "VALUES ('restore_generation','0','1970-01-01T00:00:00Z',1)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM system_metadata WHERE key='restore_generation'")
    op.execute("DELETE FROM enum_registry WHERE enum_name IN ('EventStatus','DeliveryStatus')")
    for table in (
        "audit_record",
        "delivery_attempt",
        "notification_delivery_state",
        "notification_intent",
        "current_event_projection",
        "event_evidence",
        "event_version",
        "market_event",
        "analysis_commit",
    ):
        op.execute(f"DROP TABLE {table}")
