"""Create M2 reference and acquisition tables.

Revision ID: 0002_reference_and_acquisition
Revises: 0001_m1_foundation
"""

# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0002_reference_and_acquisition"
down_revision: str | None = "0001_m1_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    statements = (
        """
        CREATE TABLE instrument (
            instrument_uid TEXT PRIMARY KEY,
            instrument_kind TEXT NOT NULL CHECK(instrument_kind IN ('STOCK','ETF','INDEX')),
            created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE instrument_identity_version (
            identity_version_uid TEXT PRIMARY KEY,
            instrument_uid TEXT NOT NULL REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK(version > 0),
            exchange TEXT NOT NULL,
            trading_code TEXT NOT NULL,
            name TEXT NOT NULL,
            listing_status TEXT NOT NULL,
            trading_status TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            UNIQUE(instrument_uid, version),
            UNIQUE(exchange, trading_code, valid_from),
            CHECK(valid_until IS NULL OR valid_until > valid_from)
        ) STRICT
        """,
        """
        CREATE TABLE sector (
            sector_uid TEXT PRIMARY KEY,
            sector_kind TEXT NOT NULL CHECK(sector_kind IN ('INDUSTRY','CONCEPT')),
            created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE sector_version (
            sector_version_uid TEXT PRIMARY KEY,
            sector_uid TEXT NOT NULL REFERENCES sector(sector_uid) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK(version > 0),
            name TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            UNIQUE(sector_uid, version),
            CHECK(valid_until IS NULL OR valid_until > valid_from)
        ) STRICT
        """,
        """
        CREATE TABLE sector_alias (
            sector_uid TEXT NOT NULL REFERENCES sector(sector_uid) ON DELETE RESTRICT,
            alias TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            PRIMARY KEY(sector_uid, alias, valid_from),
            CHECK(valid_until IS NULL OR valid_until > valid_from)
        ) STRICT
        """,
        """
        CREATE TABLE sector_membership_version (
            membership_version_uid TEXT PRIMARY KEY,
            sector_uid TEXT NOT NULL REFERENCES sector(sector_uid) ON DELETE RESTRICT,
            trading_date TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            valid_from TEXT NOT NULL,
            frozen_at TEXT NOT NULL,
            rewarm_required INTEGER NOT NULL CHECK(rewarm_required IN (0,1)),
            reason TEXT NOT NULL,
            UNIQUE(sector_uid, trading_date, version)
        ) STRICT
        """,
        """
        CREATE TABLE sector_membership (
            membership_version_uid TEXT NOT NULL
                REFERENCES sector_membership_version(membership_version_uid) ON DELETE RESTRICT,
            instrument_uid TEXT NOT NULL REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            member_role TEXT NOT NULL,
            PRIMARY KEY(membership_version_uid, instrument_uid)
        ) STRICT
        """,
        """
        CREATE TABLE analysis_subject (
            subject_uid TEXT PRIMARY KEY,
            subject_kind TEXT NOT NULL CHECK(subject_kind IN ('MARKET','SECTOR','INSTRUMENT')),
            instrument_uid TEXT REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            sector_uid TEXT REFERENCES sector(sector_uid) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            CHECK(
                (subject_kind = 'MARKET' AND instrument_uid IS NULL AND sector_uid IS NULL) OR
                (subject_kind = 'SECTOR' AND instrument_uid IS NULL AND sector_uid IS NOT NULL) OR
                (subject_kind = 'INSTRUMENT' AND instrument_uid IS NOT NULL AND sector_uid IS NULL)
            )
        ) STRICT
        """,
        "CREATE UNIQUE INDEX analysis_subject_market_uidx ON analysis_subject(subject_kind) WHERE subject_kind='MARKET'",
        "CREATE UNIQUE INDEX analysis_subject_instrument_uidx ON analysis_subject(instrument_uid) WHERE instrument_uid IS NOT NULL",
        "CREATE UNIQUE INDEX analysis_subject_sector_uidx ON analysis_subject(sector_uid) WHERE sector_uid IS NOT NULL",
        """
        CREATE TABLE trading_calendar_day (
            exchange TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            is_trading_day INTEGER NOT NULL CHECK(is_trading_day IN (0,1)),
            timezone TEXT NOT NULL,
            PRIMARY KEY(exchange, trading_date)
        ) STRICT
        """,
        """
        CREATE TABLE trading_session (
            exchange TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            phase TEXT NOT NULL,
            opens_at TEXT NOT NULL,
            closes_at TEXT NOT NULL,
            PRIMARY KEY(exchange, trading_date, sequence),
            FOREIGN KEY(exchange, trading_date)
                REFERENCES trading_calendar_day(exchange, trading_date) ON DELETE RESTRICT,
            CHECK(closes_at > opens_at)
        ) STRICT
        """,
        """
        CREATE TABLE provider_mapping (
            mapping_uid TEXT PRIMARY KEY,
            provider_key TEXT NOT NULL,
            external_code TEXT NOT NULL,
            entity_kind TEXT NOT NULL CHECK(entity_kind IN ('INSTRUMENT','SECTOR')),
            instrument_uid TEXT REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            sector_uid TEXT REFERENCES sector(sector_uid) ON DELETE RESTRICT,
            mapping_status TEXT NOT NULL CHECK(mapping_status IN ('RESOLVED','CONFLICT')),
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            UNIQUE(provider_key, external_code, valid_from),
            CHECK(valid_until IS NULL OR valid_until > valid_from),
            CHECK(
                (mapping_status = 'CONFLICT' AND instrument_uid IS NULL AND sector_uid IS NULL) OR
                (mapping_status = 'RESOLVED' AND entity_kind = 'INSTRUMENT' AND
                    instrument_uid IS NOT NULL AND sector_uid IS NULL) OR
                (mapping_status = 'RESOLVED' AND entity_kind = 'SECTOR' AND
                    instrument_uid IS NULL AND sector_uid IS NOT NULL)
            )
        ) STRICT
        """,
        "CREATE INDEX provider_mapping_lookup_idx ON provider_mapping(provider_key,external_code,valid_until)",
        """
        CREATE TABLE market_source_epoch (
            epoch_uid TEXT PRIMARY KEY,
            provider_key TEXT NOT NULL,
            capability_fingerprint TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE','RETIRED')),
            rewarm_required INTEGER NOT NULL CHECK(rewarm_required IN (0,1)),
            CHECK(ended_at IS NULL OR ended_at > started_at)
        ) STRICT
        """,
        "CREATE UNIQUE INDEX market_source_epoch_active_uidx ON market_source_epoch(provider_key) WHERE status='ACTIVE'",
        """
        CREATE TABLE market_data_batch (
            batch_uid TEXT PRIMARY KEY,
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            provider_batch_id TEXT NOT NULL,
            raw_artifact_sha256 TEXT NOT NULL REFERENCES artifact_object(sha256) ON DELETE RESTRICT,
            received_at TEXT NOT NULL,
            source_started_raw TEXT,
            source_ended_raw TEXT,
            status TEXT NOT NULL CHECK(status IN ('RECEIVED','NORMALIZED','QUARANTINED')),
            UNIQUE(epoch_uid, provider_batch_id)
        ) STRICT
        """,
        """
        CREATE TABLE raw_market_record (
            batch_uid TEXT NOT NULL REFERENCES market_data_batch(batch_uid) ON DELETE RESTRICT,
            record_index INTEGER NOT NULL CHECK(record_index >= 0),
            external_code TEXT NOT NULL,
            raw_source_time TEXT,
            payload_locator TEXT NOT NULL,
            mapping_status TEXT NOT NULL CHECK(mapping_status IN ('RESOLVED','CONFLICT','UNMAPPED')),
            PRIMARY KEY(batch_uid, record_index)
        ) STRICT
        """,
        """
        CREATE TRIGGER raw_market_record_immutable_update BEFORE UPDATE ON raw_market_record
        BEGIN SELECT RAISE(ABORT, 'raw_market_record is immutable'); END
        """,
        """
        CREATE TRIGGER raw_market_record_immutable_delete BEFORE DELETE ON raw_market_record
        BEGIN SELECT RAISE(ABORT, 'raw_market_record is immutable'); END
        """,
        """
        CREATE TABLE quote_lineage (
            lineage_uid TEXT PRIMARY KEY,
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            instrument_uid TEXT NOT NULL REFERENCES instrument(instrument_uid) ON DELETE RESTRICT,
            source_time TEXT NOT NULL,
            quote_kind TEXT NOT NULL,
            business_key_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(business_key_sha256)=64 AND business_key_sha256 NOT GLOB '*[^0-9a-f]*'),
            UNIQUE(epoch_uid, instrument_uid, source_time, quote_kind)
        ) STRICT
        """,
        """
        CREATE TRIGGER quote_lineage_immutable_update BEFORE UPDATE ON quote_lineage
        BEGIN SELECT RAISE(ABORT, 'quote_lineage is immutable'); END
        """,
        """
        CREATE TRIGGER quote_lineage_immutable_delete BEFORE DELETE ON quote_lineage
        BEGIN SELECT RAISE(ABORT, 'quote_lineage is immutable'); END
        """,
        """
        CREATE TABLE market_quote (
            quote_uid TEXT PRIMARY KEY,
            lineage_uid TEXT NOT NULL REFERENCES quote_lineage(lineage_uid) ON DELETE RESTRICT,
            record_version INTEGER NOT NULL CHECK(record_version > 0),
            batch_uid TEXT NOT NULL,
            record_index INTEGER NOT NULL,
            received_at TEXT NOT NULL,
            source_time_raw TEXT,
            source_time TEXT,
            price_scaled INTEGER,
            price_scale INTEGER NOT NULL CHECK(price_scale >= 0),
            price_status TEXT NOT NULL CHECK(price_status IN ('VALUE','MISSING','NOT_APPLICABLE','STALE','INVALID')),
            volume INTEGER,
            volume_status TEXT NOT NULL CHECK(volume_status IN ('VALUE','MISSING','NOT_APPLICABLE','STALE','INVALID')),
            is_current INTEGER NOT NULL CHECK(is_current IN (0,1)),
            UNIQUE(lineage_uid, record_version),
            FOREIGN KEY(batch_uid, record_index)
                REFERENCES raw_market_record(batch_uid, record_index) ON DELETE RESTRICT,
            CHECK((price_status='VALUE') = (price_scaled IS NOT NULL)),
            CHECK((volume_status='VALUE') = (volume IS NOT NULL))
        ) STRICT
        """,
        "CREATE UNIQUE INDEX market_quote_current_uidx ON market_quote(lineage_uid) WHERE is_current=1",
        "CREATE INDEX market_quote_source_time_idx ON market_quote(source_time)",
        """
        CREATE TABLE quote_quality_issue (
            quote_uid TEXT NOT NULL REFERENCES market_quote(quote_uid) ON DELETE RESTRICT,
            issue_code TEXT NOT NULL,
            detail TEXT NOT NULL,
            PRIMARY KEY(quote_uid, issue_code)
        ) STRICT
        """,
        """
        CREATE TABLE capability_health_report (
            report_uid TEXT PRIMARY KEY,
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            capability TEXT NOT NULL,
            health_status TEXT NOT NULL CHECK(health_status IN ('HEALTHY','DEGRADED','UNHEALTHY','UNKNOWN')),
            fitness_status TEXT NOT NULL CHECK(fitness_status IN ('FIT','FIT_WITH_LIMITATIONS','UNFIT','UNKNOWN')),
            coverage_ppm INTEGER NOT NULL CHECK(coverage_ppm BETWEEN 0 AND 1000000),
            latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
            observed_at TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            CHECK(valid_until > observed_at),
            CHECK(health_status != 'UNHEALTHY' OR fitness_status IN ('UNFIT','UNKNOWN'))
        ) STRICT
        """,
        "CREATE INDEX capability_health_lookup_idx ON capability_health_report(epoch_uid,capability,observed_at)",
        """
        CREATE TABLE capability_watermark (
            epoch_uid TEXT NOT NULL REFERENCES market_source_epoch(epoch_uid) ON DELETE RESTRICT,
            capability TEXT NOT NULL,
            event_time TEXT,
            received_time TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            rewarm_required INTEGER NOT NULL CHECK(rewarm_required IN (0,1)),
            PRIMARY KEY(epoch_uid, capability)
        ) STRICT
        """,
    )
    for statement in statements:
        op.execute(statement)

    op.execute(
        """
        INSERT INTO enum_registry(enum_name,enum_value,ordinal,source_version,active) VALUES
        ('InstrumentKind','STOCK',0,'Consolidated Architecture Baseline v1.0',1),
        ('InstrumentKind','ETF',1,'Consolidated Architecture Baseline v1.0',1),
        ('InstrumentKind','INDEX',2,'Consolidated Architecture Baseline v1.0',1),
        ('SectorKind','INDUSTRY',0,'Consolidated Architecture Baseline v1.0',1),
        ('SectorKind','CONCEPT',1,'Consolidated Architecture Baseline v1.0',1),
        ('SubjectKind','MARKET',0,'Consolidated Architecture Baseline v1.0',1),
        ('SubjectKind','SECTOR',1,'Consolidated Architecture Baseline v1.0',1),
        ('SubjectKind','INSTRUMENT',2,'Consolidated Architecture Baseline v1.0',1),
        ('FieldValueStatus','VALUE',0,'Consolidated Architecture Baseline v1.0',1),
        ('FieldValueStatus','MISSING',1,'Consolidated Architecture Baseline v1.0',1),
        ('FieldValueStatus','NOT_APPLICABLE',2,'Consolidated Architecture Baseline v1.0',1),
        ('FieldValueStatus','STALE',3,'Consolidated Architecture Baseline v1.0',1),
        ('FieldValueStatus','INVALID',4,'Consolidated Architecture Baseline v1.0',1),
        ('DataHealthStatus','HEALTHY',0,'Consolidated Architecture Baseline v1.0',1),
        ('DataHealthStatus','DEGRADED',1,'Consolidated Architecture Baseline v1.0',1),
        ('DataHealthStatus','UNHEALTHY',2,'Consolidated Architecture Baseline v1.0',1),
        ('DataHealthStatus','UNKNOWN',3,'Consolidated Architecture Baseline v1.0',1),
        ('FitnessStatus','FIT',0,'Consolidated Architecture Baseline v1.0',1),
        ('FitnessStatus','FIT_WITH_LIMITATIONS',1,'Consolidated Architecture Baseline v1.0',1),
        ('FitnessStatus','UNFIT',2,'Consolidated Architecture Baseline v1.0',1),
        ('FitnessStatus','UNKNOWN',3,'Consolidated Architecture Baseline v1.0',1)
        """
    )


def downgrade() -> None:
    for name in (
        "capability_watermark",
        "capability_health_report",
        "quote_quality_issue",
        "market_quote",
        "quote_lineage",
        "raw_market_record",
        "market_data_batch",
        "market_source_epoch",
        "provider_mapping",
        "trading_session",
        "trading_calendar_day",
        "analysis_subject",
        "sector_membership",
        "sector_membership_version",
        "sector_alias",
        "sector_version",
        "sector",
        "instrument_identity_version",
        "instrument",
    ):
        op.execute(f"DROP TABLE {name}")
    op.execute(
        """
        DELETE FROM enum_registry WHERE enum_name IN
        ('InstrumentKind','SectorKind','SubjectKind','FieldValueStatus','DataHealthStatus','FitnessStatus')
        """
    )
