"""Persist CR-004 catch-up telemetry and listing-effective eligibility evidence.

Revision ID: 0013_cr004_catchup_listing_eligibility
Revises: 0012_cr005_activation_evidence
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_cr004_catchup_listing_eligibility"
down_revision: str | None = "0012_cr005_activation_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE instrument_identity_version ADD COLUMN listing_effective_at TEXT")
    op.execute(
        "ALTER TABLE instrument_identity_version ADD COLUMN listing_evidence_sha256 TEXT "
        "REFERENCES artifact_object(sha256) ON DELETE RESTRICT"
    )
    op.execute("ALTER TABLE instrument_identity_version ADD COLUMN listing_source_ref TEXT")
    op.execute(
        """
        CREATE TABLE tdx_historical_catchup_run (
            catchup_uid TEXT PRIMARY KEY CHECK(length(catchup_uid)=36),
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            observed_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('RUNNING','FIT','WARMING_UP','FAILED')),
            coverage_checked INTEGER NOT NULL CHECK(coverage_checked>=0),
            symbols_skipped INTEGER NOT NULL CHECK(symbols_skipped>=0),
            fetch_requests INTEGER NOT NULL CHECK(fetch_requests>=0),
            fetched_bars INTEGER NOT NULL CHECK(fetched_bars>=0),
            inserted_bars INTEGER NOT NULL CHECK(inserted_bars>=0),
            duplicate_bars INTEGER NOT NULL CHECK(duplicate_bars>=0),
            missing_symbols INTEGER NOT NULL CHECK(missing_symbols>=0),
            catchup_duration_ms INTEGER NOT NULL CHECK(catchup_duration_ms>=0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX tdx_historical_catchup_epoch_idx "
        "ON tdx_historical_catchup_run(epoch_uid,observed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX tdx_historical_catchup_epoch_idx")
    op.execute("DROP TABLE tdx_historical_catchup_run")
    op.execute("ALTER TABLE instrument_identity_version DROP COLUMN listing_source_ref")
    op.execute("ALTER TABLE instrument_identity_version DROP COLUMN listing_evidence_sha256")
    op.execute("ALTER TABLE instrument_identity_version DROP COLUMN listing_effective_at")
