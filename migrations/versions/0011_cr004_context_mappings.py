"""Add immutable Owner-approved CR-004 sector context mapping versions.

Revision ID: 0011_cr004_context_mappings
Revises: 0010_cr005_threshold_registry
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_cr004_context_mappings"
down_revision: str | None = "0010_cr005_threshold_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sector_context_mapping_version (
            mapping_version_uid TEXT PRIMARY KEY
                CHECK(length(mapping_version_uid)=36),
            sector_uid TEXT NOT NULL REFERENCES sector(sector_uid) ON DELETE RESTRICT,
            etf_instrument_uid TEXT REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            style_instrument_uid TEXT REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            owner_approval_ref TEXT NOT NULL CHECK(length(owner_approval_ref)>0),
            source_artifact_sha256 TEXT NOT NULL
                REFERENCES artifact_object(sha256) ON DELETE RESTRICT,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            created_at TEXT NOT NULL,
            CHECK(etf_instrument_uid IS NOT NULL OR style_instrument_uid IS NOT NULL),
            CHECK(valid_until IS NULL OR valid_until>valid_from),
            UNIQUE(sector_uid,valid_from)
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX sector_context_mapping_lookup_idx "
        "ON sector_context_mapping_version(sector_uid,valid_from)"
    )
    op.execute(
        """
        CREATE TRIGGER sector_context_mapping_version_immutable
        BEFORE UPDATE ON sector_context_mapping_version
        BEGIN
            SELECT RAISE(ABORT,'sector context mapping version is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER sector_context_mapping_version_no_delete
        BEFORE DELETE ON sector_context_mapping_version
        BEGIN
            SELECT RAISE(ABORT,'sector context mapping version cannot be deleted');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER sector_context_mapping_version_no_delete")
    op.execute("DROP TRIGGER sector_context_mapping_version_immutable")
    op.execute("DROP TABLE sector_context_mapping_version")
