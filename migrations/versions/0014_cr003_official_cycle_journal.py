"""Add the durable, non-business CR-003 official cycle journal.

Revision ID: 0014_cr003_official_cycle_journal
Revises: 0013_cr004_catchup_listing_eligibility
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_cr003_official_cycle_journal"
down_revision: str | None = "0013_cr004_catchup_listing_eligibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE official_cycle_run (
            cycle_uid TEXT PRIMARY KEY CHECK(length(cycle_uid)=36),
            cycle_key TEXT NOT NULL,
            subject_uid TEXT NOT NULL
                REFERENCES analysis_subject(subject_uid) ON DELETE RESTRICT,
            observed_at TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN (
                'SNAPSHOT','METRIC','ANALYSIS_COMMIT','EVENT_OUTBOX','COMPLETED','FAILED'
            )),
            status TEXT NOT NULL CHECK(status IN ('RUNNING','COMMITTED','FAILED')),
            attempt INTEGER NOT NULL CHECK(attempt>=1),
            snapshot_uid TEXT REFERENCES evaluation_snapshot(snapshot_uid) ON DELETE RESTRICT,
            commit_uid TEXT REFERENCES analysis_commit(commit_uid) ON DELETE RESTRICT,
            error_code TEXT,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(cycle_key,subject_uid)
        ) STRICT
        """
    )
    op.execute(
        "CREATE INDEX official_cycle_run_status_idx "
        "ON official_cycle_run(status,updated_at DESC,cycle_uid)"
    )
    op.execute(
        "CREATE INDEX official_cycle_run_subject_idx "
        "ON official_cycle_run(subject_uid,updated_at DESC,cycle_uid)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX official_cycle_run_subject_idx")
    op.execute("DROP INDEX official_cycle_run_status_idx")
    op.execute("DROP TABLE official_cycle_run")
