"""Create M1 infrastructure tables.

Revision ID: 0001_m1_foundation
Revises:
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_m1_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE schema_migrations (
            revision TEXT PRIMARY KEY,
            checksum TEXT NOT NULL CHECK(length(checksum) = 64),
            applied_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE enum_registry (
            enum_name TEXT NOT NULL,
            enum_value TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            source_version TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0, 1)),
            PRIMARY KEY(enum_name, enum_value),
            UNIQUE(enum_name, ordinal)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE code_registry (
            code_set TEXT NOT NULL,
            code TEXT NOT NULL,
            description TEXT NOT NULL,
            source_version TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0, 1)),
            PRIMARY KEY(code_set, code)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE system_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0)
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE artifact_object (
            sha256 TEXT PRIMARY KEY
                CHECK(length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            media_type TEXT NOT NULL CHECK(length(media_type) > 0),
            relative_path TEXT NOT NULL UNIQUE CHECK(length(relative_path) > 0),
            created_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TABLE artifact_lease (
            lease_uid TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL REFERENCES artifact_object(sha256) ON DELETE RESTRICT,
            purpose TEXT NOT NULL CHECK(length(purpose) > 0),
            valid_until TEXT NOT NULL,
            created_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute("CREATE INDEX artifact_lease_sha256_idx ON artifact_lease(sha256)")
    op.execute("CREATE INDEX artifact_lease_valid_until_idx ON artifact_lease(valid_until)")
    op.execute(
        """
        CREATE TRIGGER artifact_object_immutable
        BEFORE UPDATE ON artifact_object
        BEGIN
            SELECT RAISE(ABORT, 'artifact_object is immutable');
        END
        """
    )
    op.execute(
        """
        INSERT INTO enum_registry
            (enum_name, enum_value, ordinal, source_version, active)
        VALUES
            ('ArtifactKind', 'RAW_PAYLOAD', 0,
             'Consolidated Architecture Baseline v1.0', 1),
            ('ArtifactKind', 'INPUT_MANIFEST', 1,
             'Consolidated Architecture Baseline v1.0', 1),
            ('ArtifactKind', 'BACKUP_MANIFEST', 2,
             'Consolidated Architecture Baseline v1.0', 1),
            ('ArtifactKind', 'EXPORT', 3,
             'Consolidated Architecture Baseline v1.0', 1)
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER artifact_object_immutable")
    op.execute("DROP TABLE artifact_lease")
    op.execute("DROP TABLE artifact_object")
    op.execute("DROP TABLE system_metadata")
    op.execute("DROP TABLE code_registry")
    op.execute("DROP TABLE enum_registry")
    op.execute("DROP TABLE schema_migrations")
