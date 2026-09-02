"""Add immutable Native TDX Concept/Theme membership lineage.

Revision ID: 0009_cr004_market_metric_lineage
Revises: 0008_cr002_native_tdx
"""

from collections.abc import Sequence

from alembic import op


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tdx_sector_membership_source (
            membership_version_uid TEXT PRIMARY KEY
                REFERENCES sector_membership_version(membership_version_uid) ON DELETE RESTRICT,
            block_version_uid TEXT NOT NULL
                REFERENCES tdx_block_artifact_version(block_version_uid) ON DELETE RESTRICT,
            membership_kind TEXT NOT NULL CHECK(membership_kind IN (
                'ConceptMembership','ThemeMembership'
            )),
            block_name TEXT NOT NULL,
            block_type INTEGER NOT NULL
        ) STRICT
        """
    )
    op.execute(
        """
        CREATE TRIGGER tdx_sector_membership_source_no_update
        BEFORE UPDATE ON tdx_sector_membership_source
        BEGIN
            SELECT RAISE(ABORT, 'tdx sector membership source is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER tdx_sector_membership_source_no_delete
        BEFORE DELETE ON tdx_sector_membership_source
        BEGIN
            SELECT RAISE(ABORT, 'tdx sector membership source is append-only');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER tdx_sector_membership_source_no_delete")
    op.execute("DROP TRIGGER tdx_sector_membership_source_no_update")
    op.execute("DROP TABLE tdx_sector_membership_source")


revision: str = "0009_cr004_market_metric_lineage"
down_revision: str | None = "0008_cr002_native_tdx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
