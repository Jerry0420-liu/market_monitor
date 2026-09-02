"""Create M7 OWNER security, idempotency, query, and safe settings tables.

Revision ID: 0007_api_security
Revises: 0006_events_notifications
"""

# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0007_api_security"
down_revision: str | None = "0006_events_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE owner_account (
            owner_uid TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE CHECK(username='owner'),
            password_algorithm TEXT NOT NULL CHECK(password_algorithm='SCRYPT'),
            password_salt TEXT NOT NULL CHECK(length(password_salt)=32),
            password_hash TEXT NOT NULL CHECK(length(password_hash)=128),
            scrypt_n INTEGER NOT NULL CHECK(scrypt_n>=16384),
            scrypt_r INTEGER NOT NULL CHECK(scrypt_r>=8),
            scrypt_p INTEGER NOT NULL CHECK(scrypt_p>=1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE owner_session (
            session_uid TEXT PRIMARY KEY,
            owner_uid TEXT NOT NULL REFERENCES owner_account(owner_uid) ON DELETE RESTRICT,
            token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
            csrf_hash TEXT NOT NULL CHECK(length(csrf_hash)=64),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked_at TEXT,
            CHECK(expires_at>created_at)
        ) STRICT
        """
    )
    op.execute("CREATE INDEX owner_session_owner_expiry_idx ON owner_session(owner_uid,expires_at)")
    op.execute(
        """
        CREATE TABLE login_throttle (
            identity_hash TEXT PRIMARY KEY CHECK(length(identity_hash)=64),
            failure_count INTEGER NOT NULL CHECK(failure_count>=0),
            window_started_at TEXT NOT NULL,
            blocked_until TEXT,
            updated_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE api_idempotency (
            owner_uid TEXT NOT NULL REFERENCES owner_account(owner_uid) ON DELETE RESTRICT,
            operation TEXT NOT NULL CHECK(length(operation)>0),
            key_hash TEXT NOT NULL CHECK(length(key_hash)=64),
            request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
            operation_uid TEXT NOT NULL UNIQUE CHECK(length(operation_uid)=36),
            operation_state TEXT NOT NULL CHECK(operation_state IN ('IN_PROGRESS','COMPLETED')),
            lease_uid TEXT CHECK(lease_uid IS NULL OR length(lease_uid)=36),
            lease_expires_at TEXT,
            attempt_count INTEGER NOT NULL CHECK(attempt_count>0),
            response_status INTEGER CHECK(response_status IS NULL OR response_status BETWEEN 200 AND 299),
            response_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            expires_at TEXT NOT NULL,
            CHECK(expires_at>created_at),
            CHECK(
                (operation_state='IN_PROGRESS' AND lease_uid IS NOT NULL
                    AND lease_expires_at IS NOT NULL AND lease_expires_at>updated_at
                    AND response_status IS NULL AND response_json IS NULL
                    AND completed_at IS NULL)
                OR
                (operation_state='COMPLETED' AND response_status IS NOT NULL
                    AND response_json IS NOT NULL AND completed_at IS NOT NULL)
            ),
            PRIMARY KEY(owner_uid,operation,key_hash)
        ) STRICT
        """
    )
    op.execute("CREATE INDEX api_idempotency_expiry_idx ON api_idempotency(expires_at)")
    op.execute(
        """
        CREATE TABLE analysis_query_record (
            query_uid TEXT PRIMARY KEY,
            owner_uid TEXT NOT NULL REFERENCES owner_account(owner_uid) ON DELETE RESTRICT,
            subject_uid TEXT NOT NULL REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            source_snapshot_uid TEXT NOT NULL REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            query_snapshot_uid TEXT REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            state_evaluation_uid TEXT REFERENCES state_evaluation(evaluation_uid) ON DELETE RESTRICT,
            guardian_uid TEXT REFERENCES guardian_evaluation(guardian_uid) ON DELETE RESTRICT,
            scout_uid TEXT REFERENCES scout_evaluation(scout_uid) ON DELETE RESTRICT,
            query_status TEXT NOT NULL CHECK(query_status IN ('IN_PROGRESS','COMPLETED','FAILED')),
            response_hash TEXT CHECK(response_hash IS NULL OR length(response_hash)=64),
            response_json TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(
                (query_status='IN_PROGRESS' AND query_snapshot_uid IS NULL
                    AND state_evaluation_uid IS NULL AND guardian_uid IS NULL
                    AND scout_uid IS NULL AND response_hash IS NULL
                    AND response_json IS NULL AND error_code IS NULL
                    AND completed_at IS NULL)
                OR
                (query_status='COMPLETED' AND query_snapshot_uid IS NOT NULL
                    AND state_evaluation_uid IS NOT NULL AND guardian_uid IS NOT NULL
                    AND scout_uid IS NOT NULL AND response_hash IS NOT NULL
                    AND response_json IS NOT NULL AND error_code IS NULL
                    AND completed_at IS NOT NULL)
                OR
                (query_status='FAILED' AND response_hash IS NULL
                    AND response_json IS NULL AND error_code IS NOT NULL
                    AND completed_at IS NOT NULL)
            )
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX analysis_query_owner_time_idx ON analysis_query_record(owner_uid,created_at)"
    )
    op.execute(
        """
        CREATE TABLE notification_setting (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            endpoint_source TEXT NOT NULL CHECK(endpoint_source='ENVIRONMENT'),
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TRIGGER api_idempotency_state_update
        BEFORE UPDATE ON api_idempotency
        WHEN NOT (
            NEW.owner_uid IS OLD.owner_uid
            AND NEW.operation IS OLD.operation
            AND NEW.key_hash IS OLD.key_hash
            AND NEW.request_hash IS OLD.request_hash
            AND NEW.operation_uid IS OLD.operation_uid
            AND NEW.created_at IS OLD.created_at
            AND NEW.expires_at IS OLD.expires_at
            AND OLD.operation_state='IN_PROGRESS'
            AND (
                (NEW.operation_state='IN_PROGRESS'
                    AND OLD.lease_expires_at<=NEW.updated_at
                    AND NEW.lease_uid IS NOT OLD.lease_uid
                    AND NEW.attempt_count=OLD.attempt_count+1
                    AND NEW.updated_at>=OLD.updated_at)
                OR
                (NEW.operation_state='COMPLETED'
                    AND NEW.lease_uid IS OLD.lease_uid
                    AND NEW.lease_expires_at IS OLD.lease_expires_at
                    AND NEW.attempt_count=OLD.attempt_count
                    AND NEW.updated_at>=OLD.updated_at
                    AND NEW.completed_at IS NEW.updated_at)
            )
        )
        BEGIN SELECT RAISE(ABORT,'api_idempotency state transition is invalid'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER analysis_query_record_state_update
        BEFORE UPDATE ON analysis_query_record
        WHEN NOT (
            NEW.query_uid IS OLD.query_uid
            AND NEW.owner_uid IS OLD.owner_uid
            AND NEW.subject_uid IS OLD.subject_uid
            AND NEW.source_snapshot_uid IS OLD.source_snapshot_uid
            AND NEW.created_at IS OLD.created_at
            AND NEW.updated_at>=OLD.updated_at
            AND (
                (OLD.query_status='IN_PROGRESS' AND NEW.query_status IN ('COMPLETED','FAILED'))
                OR
                (OLD.query_status='FAILED' AND NEW.query_status='IN_PROGRESS')
            )
        )
        BEGIN SELECT RAISE(ABORT,'analysis_query_record state transition is invalid'); END
        """
    )
    for table in ("api_idempotency", "analysis_query_record"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END"
        )
    op.execute(
        "INSERT INTO notification_setting(singleton,enabled,endpoint_source,updated_at,version) "
        "VALUES (1,0,'ENVIRONMENT','1970-01-01T00:00:00Z',1)"
    )
    op.execute(
        "INSERT INTO enum_registry(enum_name,enum_value,ordinal,source_version,active) "
        "VALUES ('AccountRole','OWNER',0,'API Contract v1.2',1)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM enum_registry WHERE enum_name='AccountRole'")
    for table in (
        "notification_setting",
        "analysis_query_record",
        "api_idempotency",
        "login_throttle",
        "owner_session",
        "owner_account",
    ):
        op.execute(f"DROP TABLE {table}")
