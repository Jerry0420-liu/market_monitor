"""Create additive storage for the CR-002 Native TDX provider.

Revision ID: 0008_cr002_native_tdx
Revises: 0007_api_security
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_cr002_native_tdx"
down_revision: str | None = "0007_api_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    statements = (
        """
        CREATE TABLE tdx_symbol_quarantine (
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            market INTEGER NOT NULL CHECK(market IN (0,1)),
            external_code TEXT NOT NULL CHECK(length(external_code)=6),
            failure_reason TEXT NOT NULL,
            failure_count INTEGER NOT NULL CHECK(failure_count>0),
            last_failure_at TEXT NOT NULL,
            last_success_at TEXT,
            retry_after TEXT NOT NULL,
            PRIMARY KEY(epoch_uid,market,external_code)
        ) STRICT
        """,
        """
        CREATE TABLE tdx_quote_detail (
            quote_uid TEXT PRIMARY KEY REFERENCES market_quote(quote_uid) ON DELETE RESTRICT,
            pre_close_scaled INTEGER,
            open_scaled INTEGER,
            high_scaled INTEGER,
            low_scaled INTEGER,
            price_scale INTEGER NOT NULL CHECK(price_scale>=0),
            amount_scaled INTEGER,
            amount_scale INTEGER NOT NULL CHECK(amount_scale>=0),
            quote_status TEXT NOT NULL CHECK(quote_status IN ('VALUE','NO_VALID_QUOTE')),
            server_time_raw INTEGER NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE tdx_bar (
            bar_uid TEXT PRIMARY KEY CHECK(length(bar_uid)=36),
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            instrument_uid TEXT NOT NULL REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            interval_kind TEXT NOT NULL CHECK(interval_kind IN ('1m','1d')),
            source_time TEXT NOT NULL,
            open_scaled INTEGER NOT NULL,
            high_scaled INTEGER NOT NULL,
            low_scaled INTEGER NOT NULL,
            close_scaled INTEGER NOT NULL,
            price_scale INTEGER NOT NULL CHECK(price_scale>=0),
            volume INTEGER NOT NULL CHECK(volume>=0),
    volume_unit TEXT NOT NULL
        CHECK(volume_unit IN ('SHARES','INDEX_SHARE_LIKE','INDEX_LOT_LIKE')),
            amount_scaled INTEGER NOT NULL,
            amount_scale INTEGER NOT NULL CHECK(amount_scale>=0),
            UNIQUE(epoch_uid,instrument_uid,interval_kind,source_time)
        ) STRICT
        """,
        "CREATE INDEX tdx_bar_lookup_idx ON tdx_bar(instrument_uid,interval_kind,source_time)",
        """
        CREATE TABLE tdx_block_artifact_version (
            block_version_uid TEXT PRIMARY KEY CHECK(length(block_version_uid)=36),
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
    filename TEXT NOT NULL
        CHECK(filename IN ('block.dat','block_zs.dat','block_gn.dat','block_fg.dat')),
            version INTEGER NOT NULL CHECK(version>0),
            source_server TEXT NOT NULL,
            byte_length INTEGER NOT NULL CHECK(byte_length>=0),
            server_hash TEXT,
            artifact_sha256 TEXT NOT NULL REFERENCES artifact_object(sha256) ON DELETE RESTRICT,
            fetched_at TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            UNIQUE(filename,version),
            UNIQUE(filename,artifact_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE tdx_block_membership (
            block_version_uid TEXT NOT NULL REFERENCES tdx_block_artifact_version(block_version_uid)
                ON DELETE RESTRICT,
            block_name TEXT NOT NULL,
            block_type INTEGER NOT NULL,
            membership_kind TEXT NOT NULL CHECK(membership_kind IN (
                'IndexMembership','ConceptMembership','ThemeMembership','StyleFactorMembership',
                'StatusMembership','TDXCuratedMembership'
            )),
            market INTEGER NOT NULL CHECK(market IN (0,1)),
            external_code TEXT NOT NULL CHECK(length(external_code)=6),
            PRIMARY KEY(block_version_uid,block_name,market,external_code)
        ) STRICT
        """,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE tdx_block_membership",
        "DROP TABLE tdx_block_artifact_version",
        "DROP INDEX tdx_bar_lookup_idx",
        "DROP TABLE tdx_bar",
        "DROP TABLE tdx_quote_detail",
        "DROP TABLE tdx_symbol_quarantine",
    ):
        op.execute(statement)
