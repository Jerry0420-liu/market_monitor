"""Create the append-only CR-005 threshold registry.

Revision ID: 0010_cr005_threshold_registry
Revises: 0009_cr004_market_metric_lineage
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

_CREATED_AT = "2026-08-24T00:00:00.000000Z"
_LEGACY_GUARDIAN_UID = "69e8d0c4-90ca-47c1-a40a-000000000001"
_LEGACY_SCOUT_UID = "69e8d0c4-90ca-47c1-a40a-000000000002"
_PROD_GUARDIAN_UID = "69e8d0c4-90ca-47c1-a40a-000000000003"
_PROD_SCOUT_UID = "69e8d0c4-90ca-47c1-a40a-000000000004"


def upgrade() -> None:
    for statement in (
        """
        CREATE TABLE threshold_version (
            threshold_version_uid TEXT PRIMARY KEY CHECK(length(threshold_version_uid)=36),
            family TEXT NOT NULL CHECK(family IN ('GUARDIAN','SCOUT')),
            version TEXT NOT NULL,
            approval_status TEXT NOT NULL CHECK(approval_status IN (
                'LEGACY_UNCALIBRATED','OWNER_APPROVED_PENDING_VALIDATION','OWNER_APPROVED'
            )),
            owner_approval_reference TEXT NOT NULL,
            definition_hash TEXT NOT NULL UNIQUE CHECK(length(definition_hash)=64),
            created_at TEXT NOT NULL,
            UNIQUE(family,version)
        ) STRICT
        """,
        """
        CREATE TABLE threshold_entry (
            threshold_entry_uid TEXT PRIMARY KEY CHECK(length(threshold_entry_uid)=36),
            threshold_version_uid TEXT NOT NULL
                REFERENCES threshold_version(threshold_version_uid) ON DELETE RESTRICT,
            metric_code TEXT NOT NULL,
            threshold_ppm INTEGER NOT NULL CHECK(threshold_ppm BETWEEN 0 AND 1000000),
            created_at TEXT NOT NULL,
            UNIQUE(threshold_version_uid,metric_code)
        ) STRICT
        """,
        """
        CREATE TABLE threshold_validation (
            validation_order INTEGER PRIMARY KEY AUTOINCREMENT,
            validation_uid TEXT NOT NULL UNIQUE CHECK(length(validation_uid)=36),
            threshold_version_uid TEXT NOT NULL
                REFERENCES threshold_version(threshold_version_uid) ON DELETE RESTRICT,
            validation_kind TEXT NOT NULL CHECK(validation_kind IN ('REPLAY','SHADOW')),
            validation_status TEXT NOT NULL CHECK(validation_status IN ('PASS','FAIL')),
            report_reference TEXT NOT NULL,
            report_sha256 TEXT NOT NULL REFERENCES artifact_object(sha256) ON DELETE RESTRICT
                CHECK(length(report_sha256)=64),
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(threshold_version_uid,validation_kind,report_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE threshold_activation (
            activation_order INTEGER PRIMARY KEY AUTOINCREMENT,
            activation_uid TEXT NOT NULL UNIQUE CHECK(length(activation_uid)=36),
            family TEXT NOT NULL CHECK(family IN ('GUARDIAN','SCOUT')),
            threshold_version_uid TEXT NOT NULL
                REFERENCES threshold_version(threshold_version_uid) ON DELETE RESTRICT,
            effective_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            owner_approval_reference TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(family,effective_at),
            UNIQUE(family,idempotency_key)
        ) STRICT
        """,
        "CREATE INDEX threshold_activation_as_of_idx "
        "ON threshold_activation(family,effective_at DESC,activation_order DESC)",
        "CREATE INDEX threshold_validation_latest_idx "
        "ON threshold_validation(threshold_version_uid,validation_kind,"
        "observed_at DESC,validation_order DESC)",
        """
        CREATE TRIGGER threshold_activation_family_match
        BEFORE INSERT ON threshold_activation
        WHEN (SELECT family FROM threshold_version
              WHERE threshold_version_uid=NEW.threshold_version_uid) != NEW.family
        BEGIN
            SELECT RAISE(ABORT,'threshold activation family mismatch');
        END
        """,
        """
        CREATE TRIGGER threshold_activation_requires_approval_and_validation
        BEFORE INSERT ON threshold_activation
        WHEN NOT EXISTS (
            SELECT 1 FROM threshold_version
            WHERE threshold_version_uid=NEW.threshold_version_uid
              AND approval_status IN ('OWNER_APPROVED_PENDING_VALIDATION','OWNER_APPROVED')
        )
        OR NOT EXISTS (
            SELECT 1
            WHERE COALESCE((
                SELECT validation_status FROM threshold_validation
                WHERE threshold_version_uid=NEW.threshold_version_uid
                  AND validation_kind='REPLAY'
                  AND observed_at<=NEW.effective_at
                ORDER BY observed_at DESC,validation_order DESC
                LIMIT 1
            ),'FAIL')='PASS'
        )
        OR NOT EXISTS (
            SELECT 1
            WHERE COALESCE((
                SELECT validation_status FROM threshold_validation
                WHERE threshold_version_uid=NEW.threshold_version_uid
                  AND validation_kind='SHADOW'
                  AND observed_at<=NEW.effective_at
                ORDER BY observed_at DESC,validation_order DESC
                LIMIT 1
            ),'FAIL')='PASS'
        )
        BEGIN
            SELECT RAISE(ABORT,'threshold activation requires validated approval');
        END
        """,
        """
        CREATE TRIGGER threshold_activation_effective_time_monotonic
        BEFORE INSERT ON threshold_activation
        WHEN EXISTS (
            SELECT 1 FROM threshold_activation
            WHERE family=NEW.family AND effective_at >= NEW.effective_at
        )
        BEGIN
            SELECT RAISE(ABORT,'threshold activation effective time must strictly advance');
        END
        """,
    ):
        op.execute(statement)

    for table in (
        "threshold_version",
        "threshold_entry",
        "threshold_validation",
        "threshold_activation",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} is append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT,'{table} is append-only'); END"
        )

    _seed(
        _LEGACY_GUARDIAN_UID,
        "GUARDIAN",
        "guardian-v1",
        "LEGACY_UNCALIBRATED",
        "legacy guardian-v1 retained for explicit replay only",
        "44dc1f8f9eb3778e8d2ffc703f5e60ec0db8636c353b77f9eb647cc3466bb860",
        {
            "RISE_RATE_PPM": 700_000,
            "HEAD_CONCENTRATION_PPM": 650_000,
            "INTERNAL_DIVERGENCE_PPM": 600_000,
            "CROWDING_PPM": 700_000,
            "LIQUIDITY_WEAKENING_PPM": 500_000,
            "CORE_WEAKENING_PPM": 600_000,
            "BREADTH_COLLAPSE_PPM": 600_000,
            "STAMPEDE_RISK_PPM": 700_000,
            "T1_CHASING_RISK_PPM": 600_000,
            "EARLY_SIGNAL_FAILURE_PPM": 500_000,
        },
        1,
    )
    _seed(
        _LEGACY_SCOUT_UID,
        "SCOUT",
        "scout-v1",
        "LEGACY_UNCALIBRATED",
        "legacy scout-v1 retained for explicit replay only",
        "3f214767a58294f15bfca339e2e7f11941ee5a43e694a686c5bb8838d73b54f7",
        {
            "EARLY_ACTIVITY_PPM": 600_000,
            "HEALTHY_BREADTH_PPM": 600_000,
            "RELATIVE_STRENGTH_PPM": 600_000,
            "TURNOVER_CONFIRMATION_PPM": 600_000,
            "ETF_CONFIRMATION_PPM": 600_000,
            "STYLE_SUPPORT_PPM": 600_000,
            "LOW_CROWDING_PPM": 600_000,
            "CONTINUITY_STRENGTHENING_PPM": 600_000,
        },
        11,
    )
    _seed(
        _PROD_GUARDIAN_UID,
        "GUARDIAN",
        "guardian-thresholds-v1.0-prod",
        "OWNER_APPROVED_PENDING_VALIDATION",
        "docs/decisions/CR-005_GUARDIAN_SCOUT_PRODUCTION_THRESHOLD_CALIBRATION.md",
        "52756668ee2766d1bee0db03903033801b9000d5b4dcdedaecf7782005fb5b0b",
        {
            "RISE_RATE_PPM": 600_000,
            "HEAD_CONCENTRATION_PPM": 600_000,
            "INTERNAL_DIVERGENCE_PPM": 550_000,
            "CROWDING_PPM": 600_000,
            "LIQUIDITY_WEAKENING_PPM": 550_000,
            "CORE_WEAKENING_PPM": 550_000,
            "BREADTH_COLLAPSE_PPM": 550_000,
            "STAMPEDE_RISK_PPM": 600_000,
            "T1_CHASING_RISK_PPM": 600_000,
            "EARLY_SIGNAL_FAILURE_PPM": 550_000,
        },
        19,
    )
    _seed(
        _PROD_SCOUT_UID,
        "SCOUT",
        "scout-thresholds-v1.0-prod",
        "OWNER_APPROVED_PENDING_VALIDATION",
        "docs/decisions/CR-005_GUARDIAN_SCOUT_PRODUCTION_THRESHOLD_CALIBRATION.md",
        "cca5873ea533bd7bce4b2be4870f46843955b2001a29a79d7ae4d849a4488ceb",
        {
            "EARLY_ACTIVITY_PPM": 650_000,
            "HEALTHY_BREADTH_PPM": 650_000,
            "RELATIVE_STRENGTH_PPM": 650_000,
            "TURNOVER_CONFIRMATION_PPM": 600_000,
            "ETF_CONFIRMATION_PPM": 550_000,
            "STYLE_SUPPORT_PPM": 550_000,
            "LOW_CROWDING_PPM": 650_000,
            "CONTINUITY_STRENGTHENING_PPM": 650_000,
        },
        29,
    )
    op.execute(
        "ALTER TABLE rule_execution ADD COLUMN threshold_version_uid TEXT "
        "REFERENCES threshold_version(threshold_version_uid) ON DELETE RESTRICT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE rule_execution DROP COLUMN threshold_version_uid")
    for table in (
        "threshold_activation",
        "threshold_validation",
        "threshold_entry",
        "threshold_version",
    ):
        op.execute(f"DROP TRIGGER {table}_immutable_delete")
        op.execute(f"DROP TRIGGER {table}_immutable_update")
    op.execute("DROP TRIGGER threshold_activation_effective_time_monotonic")
    op.execute("DROP TRIGGER threshold_activation_requires_approval_and_validation")
    op.execute("DROP TRIGGER threshold_activation_family_match")
    op.execute("DROP INDEX threshold_validation_latest_idx")
    op.execute("DROP INDEX threshold_activation_as_of_idx")
    op.execute("DROP TABLE threshold_activation")
    op.execute("DROP TABLE threshold_validation")
    op.execute("DROP TABLE threshold_entry")
    op.execute("DROP TABLE threshold_version")


def _seed(
    uid: str,
    family: str,
    version: str,
    approval_status: str,
    approval_reference: str,
    definition_hash: str,
    entries: dict[str, int],
    entry_start: int,
) -> None:
    op.execute(
        "INSERT INTO threshold_version"
        "(threshold_version_uid,family,version,approval_status,owner_approval_reference,"
        "definition_hash,created_at) "
        f"VALUES ('{uid}','{family}','{version}','{approval_status}',"
        f"'{approval_reference}','{definition_hash}','{_CREATED_AT}')"
    )
    for offset, (metric_code, threshold_ppm) in enumerate(sorted(entries.items())):
        entry_uid = f"00000000-0000-4000-8000-{entry_start + offset:012d}"
        op.execute(
            "INSERT INTO threshold_entry"
            "(threshold_entry_uid,threshold_version_uid,metric_code,threshold_ppm,created_at) "
            f"VALUES ('{entry_uid}','{uid}','{metric_code}',{threshold_ppm},'{_CREATED_AT}')"
        )


revision: str = "0010_cr005_threshold_registry"
down_revision: str | None = "0009_cr004_market_metric_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
