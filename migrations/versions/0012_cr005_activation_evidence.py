"""Require typed CR-005 production evidence for threshold activation.

Revision ID: 0012_cr005_activation_evidence
Revises: 0011_cr004_context_mappings
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_cr005_activation_evidence"
down_revision: str | None = "0011_cr004_context_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE threshold_activation_evidence (
            acceptance_evidence_uid TEXT PRIMARY KEY CHECK(length(acceptance_evidence_uid)=36),
            evidence_sha256 TEXT NOT NULL UNIQUE
                REFERENCES artifact_object(sha256) ON DELETE RESTRICT
                CHECK(length(evidence_sha256)=64),
            replay_evidence_sha256 TEXT NOT NULL
                REFERENCES artifact_object(sha256) ON DELETE RESTRICT
                CHECK(length(replay_evidence_sha256)=64),
            guardian_threshold_version_uid TEXT NOT NULL
                REFERENCES threshold_version(threshold_version_uid) ON DELETE RESTRICT,
            scout_threshold_version_uid TEXT NOT NULL
                REFERENCES threshold_version(threshold_version_uid) ON DELETE RESTRICT,
            evidence_scope TEXT NOT NULL CHECK(evidence_scope IN ('PRODUCTION','DEMO')),
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        ) STRICT
        """
    )
    op.execute(
        "ALTER TABLE threshold_activation ADD COLUMN acceptance_evidence_uid TEXT "
        "REFERENCES threshold_activation_evidence(acceptance_evidence_uid) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX threshold_activation_evidence_pair_idx ON threshold_activation_evidence("
        "guardian_threshold_version_uid,scout_threshold_version_uid,observed_at DESC)"
    )
    op.execute(
        """
        CREATE TRIGGER threshold_activation_requires_typed_evidence
        BEFORE INSERT ON threshold_activation
        WHEN NEW.acceptance_evidence_uid IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM threshold_activation_evidence e
            WHERE e.acceptance_evidence_uid=NEW.acceptance_evidence_uid
              AND e.observed_at<=NEW.effective_at
              AND (
                (NEW.family='GUARDIAN'
                    AND e.guardian_threshold_version_uid=NEW.threshold_version_uid)
                OR (NEW.family='SCOUT'
                    AND e.scout_threshold_version_uid=NEW.threshold_version_uid)
              )
        )
        BEGIN
            SELECT RAISE(ABORT,'threshold activation requires typed production evidence');
        END
        """
    )
    for table in ("threshold_activation_evidence",):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} append-only'); END"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER threshold_activation_evidence_immutable_delete")
    op.execute("DROP TRIGGER threshold_activation_evidence_immutable_update")
    op.execute("DROP TRIGGER threshold_activation_requires_typed_evidence")
    op.execute("DROP INDEX threshold_activation_evidence_pair_idx")
    op.execute("ALTER TABLE threshold_activation DROP COLUMN acceptance_evidence_uid")
    op.execute("DROP TABLE threshold_activation_evidence")
