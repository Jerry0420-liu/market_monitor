from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid, parse_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_data.models import (
    InstrumentIdentity,
    ProviderInstrumentRegistration,
    ProviderListingState,
    ReferenceCandidate,
    SectorContextMappingVersion,
)


class ReferenceNotFoundError(LookupError):
    pass


class AmbiguousReferenceError(LookupError):
    pass


@dataclass(frozen=True)
class _ProviderIdentityRow:
    instrument_uid: str
    version: int
    exchange: str
    trading_code: str
    name: str
    listing_status: str
    trading_status: str
    listing_effective_at: str | None
    listing_evidence_sha256: str | None
    listing_source_ref: str | None
    valid_from: str


class ReferenceRepository:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def create_instrument(self, kind: str, created_at: datetime) -> str:
        uid = new_uid()
        self._write(
            "INSERT INTO instrument(instrument_uid,instrument_kind,created_at) VALUES (?,?,?)",
            (uid, kind, format_rfc3339(created_at)),
        )
        return uid

    def register_provider_instruments(
        self,
        provider: str,
        records: Sequence[ProviderInstrumentRegistration],
        valid_from: datetime,
    ) -> int:
        registrations = tuple(records)
        for record in registrations:
            _validate_listing_provenance(record)
        external_codes = {record.external_code for record in registrations}
        if len(external_codes) != len(registrations):
            raise ValueError("provider registrations must have unique external codes")
        if not registrations:
            return 0
        instant = format_rfc3339(valid_from)
        existing = self._current_provider_identity_rows(provider, instant)
        missing = tuple(record for record in registrations if record.external_code not in existing)
        updates = tuple(
            record
            for record in registrations
            if record.external_code in existing
            and _identity_update_required(existing[record.external_code], record, instant)
        )
        if not missing and not updates:
            return 0

        def command(transaction: TransactionContext) -> None:
            for record in missing:
                instrument_uid = new_uid()
                transaction.connection.exec_driver_sql(
                    "INSERT INTO instrument(instrument_uid,instrument_kind,created_at) "
                    "VALUES (?,?,?)",
                    (instrument_uid, record.instrument_kind, instant),
                )
                transaction.connection.exec_driver_sql(
                    "INSERT INTO instrument_identity_version "
                    "(identity_version_uid,instrument_uid,version,exchange,trading_code,name,"
                    "listing_status,trading_status,listing_effective_at,listing_evidence_sha256,"
                    "listing_source_ref,valid_from,valid_until) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        new_uid(),
                        instrument_uid,
                        1,
                        record.exchange,
                        record.trading_code,
                        record.name,
                        record.listing_status,
                        record.trading_status,
                        _listing_effective_at(record, instant),
                        record.listing_evidence_sha256,
                        record.listing_source_ref,
                        instant,
                    ),
                )
                transaction.connection.exec_driver_sql(
                    "INSERT INTO provider_mapping"
                    "(mapping_uid,provider_key,external_code,entity_kind,instrument_uid,sector_uid,"
                    "mapping_status,valid_from,valid_until) VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (
                        new_uid(),
                        provider,
                        record.external_code,
                        "INSTRUMENT",
                        instrument_uid,
                        None,
                        "RESOLVED",
                        instant,
                    ),
                )
            for record in updates:
                previous = existing[record.external_code]
                if previous.valid_from >= instant:
                    raise ValueError("identity versions must advance effective time")
                transaction.connection.exec_driver_sql(
                    "INSERT INTO instrument_identity_version "
                    "(identity_version_uid,instrument_uid,version,exchange,trading_code,name,"
                    "listing_status,trading_status,listing_effective_at,listing_evidence_sha256,"
                    "listing_source_ref,valid_from,valid_until) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        new_uid(),
                        previous.instrument_uid,
                        previous.version + 1,
                        record.exchange,
                        record.trading_code,
                        record.name,
                        record.listing_status,
                        record.trading_status,
                        _listing_effective_at(record, instant),
                        record.listing_evidence_sha256,
                        record.listing_source_ref,
                        instant,
                    ),
                )

        self._writer.submit(command).result()
        return len(missing)

    def import_listing_reference_facts(
        self,
        provider: str,
        records: Sequence[ProviderInstrumentRegistration],
        valid_from: datetime,
        *,
        source_artifact_sha256: str,
        source_ref: str,
    ) -> int:
        """Append explicitly supplied, artifact-backed listing facts."""
        _validate_artifact_digest(source_artifact_sha256)
        if not source_ref.strip():
            raise ValueError("listing Reference facts require a source reference")
        with self._runtime.read_connection() as connection:
            if (
                connection.exec_driver_sql(
                    "SELECT 1 FROM artifact_object WHERE sha256=?",
                    (source_artifact_sha256,),
                ).scalar_one_or_none()
                is None
            ):
                raise ReferenceNotFoundError(source_artifact_sha256)
        enriched: list[ProviderInstrumentRegistration] = []
        for record in records:
            if record.listing_status not in {"LISTED", "PRE_LISTING", "NOT_YET_LISTED"}:
                raise ValueError("listing Reference fact has an invalid listing status")
            if record.listing_effective_at is None:
                raise ValueError("listing Reference fact requires an effective time")
            if record.listing_evidence_sha256 not in {None, source_artifact_sha256}:
                raise ValueError("listing Reference fact artifact does not match its source")
            if record.listing_source_ref not in {None, source_ref}:
                raise ValueError("listing Reference fact source does not match its source")
            enriched.append(
                replace(
                    record,
                    listing_evidence_sha256=source_artifact_sha256,
                    listing_source_ref=source_ref,
                )
            )
        return self.register_provider_instruments(provider, tuple(enriched), valid_from)

    def provider_listing_states(
        self, provider: str, at: datetime
    ) -> dict[str, ProviderListingState]:
        instant = format_rfc3339(at)
        return {
            external_code: ProviderListingState(
                external_code,
                row.listing_status,
                row.trading_status,
                (
                    None
                    if row.listing_effective_at is None
                    else parse_rfc3339(row.listing_effective_at)
                ),
                row.listing_evidence_sha256,
                row.listing_source_ref,
            )
            for external_code, row in self._current_provider_identity_rows(
                provider, instant
            ).items()
        }

    def _current_provider_identity_rows(
        self, provider: str, instant: str
    ) -> dict[str, _ProviderIdentityRow]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "WITH current_mapping AS ("
                "SELECT external_code,instrument_uid,row_number() OVER ("
                "PARTITION BY external_code ORDER BY valid_from DESC) AS mapping_rank "
                "FROM provider_mapping WHERE provider_key=? AND entity_kind='INSTRUMENT' "
                "AND mapping_status='RESOLVED' AND valid_from<=? "
                "AND (valid_until IS NULL OR valid_until>?)"
                "), current_identity AS ("
                "SELECT m.external_code,v.instrument_uid,v.version,v.exchange,v.trading_code,"
                "v.name,"
                "v.listing_status,v.trading_status,v.listing_effective_at,"
                "v.listing_evidence_sha256,v.listing_source_ref,v.valid_from,"
                "row_number() OVER (PARTITION BY m.external_code "
                "ORDER BY v.valid_from DESC,v.version DESC) AS identity_rank "
                "FROM current_mapping m JOIN instrument_identity_version v "
                "ON v.instrument_uid=m.instrument_uid WHERE m.mapping_rank=1 "
                "AND v.valid_from<=? AND (v.valid_until IS NULL OR v.valid_until>?)"
                ") SELECT external_code,instrument_uid,version,exchange,trading_code,name,"
                "listing_status,trading_status,listing_effective_at,listing_evidence_sha256,"
                "listing_source_ref,valid_from "
                "FROM current_identity WHERE identity_rank=1",
                (provider, instant, instant, instant, instant),
            ).all()
        return {
            str(row.external_code): _ProviderIdentityRow(
                str(row.instrument_uid),
                int(row.version),
                str(row.exchange),
                str(row.trading_code),
                str(row.name),
                str(row.listing_status),
                str(row.trading_status),
                None if row.listing_effective_at is None else str(row.listing_effective_at),
                None if row.listing_evidence_sha256 is None else str(row.listing_evidence_sha256),
                None if row.listing_source_ref is None else str(row.listing_source_ref),
                str(row.valid_from),
            )
            for row in rows
        }

    def add_instrument_identity_version(
        self,
        instrument_uid: str,
        exchange: str,
        trading_code: str,
        name: str,
        listing_status: str,
        trading_status: str,
        valid_from: datetime,
        listing_effective_at: datetime | None = None,
        listing_evidence_sha256: str | None = None,
        listing_source_ref: str | None = None,
    ) -> str:
        instant = format_rfc3339(valid_from)
        uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            latest = transaction.connection.exec_driver_sql(
                "SELECT version,valid_from FROM instrument_identity_version "
                "WHERE instrument_uid=? ORDER BY version DESC LIMIT 1",
                (instrument_uid,),
            ).one_or_none()
            if latest is not None and str(latest.valid_from) >= instant:
                raise ValueError("identity versions must advance effective time")
            version = 1 if latest is None else int(latest.version) + 1
            transaction.connection.exec_driver_sql(
                "INSERT INTO instrument_identity_version"
                "(identity_version_uid,instrument_uid,version,exchange,trading_code,name,"
                "listing_status,trading_status,listing_effective_at,listing_evidence_sha256,"
                "listing_source_ref,valid_from,valid_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    uid,
                    instrument_uid,
                    version,
                    exchange,
                    trading_code,
                    name,
                    listing_status,
                    trading_status,
                    (
                        None
                        if listing_effective_at is None
                        else format_rfc3339(listing_effective_at)
                    ),
                    listing_evidence_sha256,
                    listing_source_ref,
                    instant,
                ),
            )

        self._writer.submit(command).result()
        return uid

    def get_instrument_as_of(self, instrument_uid: str, at: datetime) -> InstrumentIdentity:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT instrument_uid,version,exchange,trading_code,name "
                "FROM instrument_identity_version WHERE instrument_uid=? AND valid_from<=? "
                "ORDER BY valid_from DESC LIMIT 1",
                (instrument_uid, format_rfc3339(at)),
            ).one_or_none()
        if row is None:
            raise ReferenceNotFoundError(instrument_uid)
        return InstrumentIdentity(
            str(row.instrument_uid),
            int(row.version),
            str(row.exchange),
            str(row.trading_code),
            str(row.name),
        )

    def create_sector(self, kind: str, created_at: datetime) -> str:
        uid = new_uid()
        self._write(
            "INSERT INTO sector(sector_uid,sector_kind,created_at) VALUES (?,?,?)",
            (uid, kind, format_rfc3339(created_at)),
        )
        return uid

    def add_sector_version(self, sector_uid: str, name: str, valid_from: datetime) -> str:
        uid = new_uid()
        instant = format_rfc3339(valid_from)

        def command(transaction: TransactionContext) -> None:
            latest = transaction.connection.exec_driver_sql(
                "SELECT version,valid_from FROM sector_version WHERE sector_uid=? "
                "ORDER BY version DESC LIMIT 1",
                (sector_uid,),
            ).one_or_none()
            if latest is not None and str(latest.valid_from) >= instant:
                raise ValueError("sector versions must advance effective time")
            version = 1 if latest is None else int(latest.version) + 1
            transaction.connection.exec_driver_sql(
                "INSERT INTO sector_version"
                "(sector_version_uid,sector_uid,version,name,valid_from,valid_until) "
                "VALUES (?,?,?,?,?,NULL)",
                (uid, sector_uid, version, name, instant),
            )

        self._writer.submit(command).result()
        return uid

    def get_sector_as_of(self, sector_uid: str, at: datetime) -> ReferenceCandidate:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT s.sector_uid,s.sector_kind,v.name FROM sector s JOIN sector_version v "
                "ON v.sector_uid=s.sector_uid WHERE s.sector_uid=? AND v.valid_from<=? "
                "ORDER BY v.valid_from DESC LIMIT 1",
                (sector_uid, format_rfc3339(at)),
            ).one_or_none()
        if row is None:
            raise ReferenceNotFoundError(sector_uid)
        return ReferenceCandidate(str(row.sector_uid), str(row.sector_kind), str(row.name), None)

    def ensure_analysis_subject(self, kind: str, target_uid: str | None = None) -> str:
        column = {"MARKET": None, "SECTOR": "sector_uid", "INSTRUMENT": "instrument_uid"}.get(kind)
        if kind != "MARKET" and column is None:
            raise ValueError("unknown subject kind")
        where = "subject_kind='MARKET'" if column is None else f"{column}=?"
        params: tuple[str, ...] = () if column is None else (str(target_uid),)
        with self._runtime.read_connection() as connection:
            existing = connection.exec_driver_sql(
                f"SELECT subject_uid FROM analysis_subject WHERE {where}", params
            ).scalar_one_or_none()
        if existing is not None:
            return str(existing)
        uid = new_uid()
        instrument = target_uid if kind == "INSTRUMENT" else None
        sector = target_uid if kind == "SECTOR" else None
        self._write(
            "INSERT INTO analysis_subject"
            "(subject_uid,subject_kind,instrument_uid,sector_uid,created_at) "
            "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (uid, kind, instrument, sector),
        )
        return uid

    def record_mapping_conflict(
        self, provider: str, external_code: str, entity_kind: str, valid_from: datetime
    ) -> str:
        return self._add_mapping(
            provider, external_code, entity_kind, None, None, "CONFLICT", valid_from
        )

    def map_instrument(
        self, provider: str, external_code: str, instrument_uid: str, valid_from: datetime
    ) -> str:
        return self._add_mapping(
            provider, external_code, "INSTRUMENT", instrument_uid, None, "RESOLVED", valid_from
        )

    def map_sector(
        self, provider: str, external_code: str, sector_uid: str, valid_from: datetime
    ) -> str:
        return self._add_mapping(
            provider, external_code, "SECTOR", None, sector_uid, "RESOLVED", valid_from
        )

    def resolve_provider_mapping(
        self, provider: str, external_code: str, at: datetime
    ) -> str | None:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT mapping_status,instrument_uid,sector_uid FROM provider_mapping "
                "WHERE provider_key=? AND external_code=? AND valid_from<=? "
                "ORDER BY valid_from DESC LIMIT 1",
                (provider, external_code, format_rfc3339(at)),
            ).one_or_none()
        if row is None or row.mapping_status != "RESOLVED":
            return None
        return str(row.instrument_uid or row.sector_uid)

    def search_reference(self, query: str) -> tuple[ReferenceCandidate, ...]:
        term = f"%{query.strip()}%"
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT i.instrument_uid AS uid,i.instrument_kind AS kind,v.name,v.trading_code "
                "FROM instrument i JOIN instrument_identity_version v "
                "ON v.instrument_uid=i.instrument_uid "
                "WHERE v.version=(SELECT max(x.version) FROM instrument_identity_version x "
                "WHERE x.instrument_uid=i.instrument_uid) "
                "AND (v.name LIKE ? OR v.trading_code LIKE ?) "
                "UNION ALL SELECT s.sector_uid,s.sector_kind,v.name,NULL FROM sector s "
                "JOIN sector_version v ON v.sector_uid=s.sector_uid "
                "WHERE v.version=(SELECT max(x.version) "
                "FROM sector_version x WHERE x.sector_uid=s.sector_uid) AND v.name LIKE ? "
                "ORDER BY name,uid",
                (term, term, term),
            ).all()
        return tuple(
            ReferenceCandidate(str(row.uid), str(row.kind), str(row.name), row.trading_code)
            for row in rows
        )

    def resolve_search(self, query: str) -> ReferenceCandidate:
        candidates = self.search_reference(query)
        if len(candidates) != 1:
            raise AmbiguousReferenceError(f"expected one candidate, got {len(candidates)}")
        return candidates[0]

    def freeze_membership(
        self,
        sector_uid: str,
        trading_date: str,
        instrument_uids: list[str],
        frozen_at: datetime,
        *,
        correction: bool = False,
    ) -> str:
        members = tuple(sorted(set(instrument_uids)))

        def command(transaction: TransactionContext) -> str:
            latest = transaction.connection.exec_driver_sql(
                "SELECT membership_version_uid,version FROM sector_membership_version "
                "WHERE sector_uid=? AND trading_date=? ORDER BY version DESC LIMIT 1",
                (sector_uid, trading_date),
            ).one_or_none()
            if latest is not None and not correction:
                return str(latest.membership_version_uid)
            uid = new_uid()
            version = 1 if latest is None else int(latest.version) + 1
            transaction.connection.exec_driver_sql(
                "INSERT INTO sector_membership_version"
                "(membership_version_uid,sector_uid,trading_date,version,valid_from,frozen_at,"
                "rewarm_required,reason) VALUES (?,?,?,?,?,?,?,?)",
                (
                    uid,
                    sector_uid,
                    trading_date,
                    version,
                    format_rfc3339(frozen_at),
                    format_rfc3339(frozen_at),
                    int(correction),
                    "CORRECTION" if correction else "DAILY_FREEZE",
                ),
            )
            for instrument_uid in members:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO sector_membership"
                    "(membership_version_uid,instrument_uid,member_role) VALUES (?,?,?)",
                    (uid, instrument_uid, "MEMBER"),
                )
            return uid

        return self._writer.submit(command).result()

    def membership_as_of(self, sector_uid: str, trading_date: str, version: int) -> tuple[str, ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT m.instrument_uid FROM sector_membership m "
                "JOIN sector_membership_version v "
                "ON v.membership_version_uid=m.membership_version_uid "
                "WHERE v.sector_uid=? AND v.trading_date=? AND v.version=? "
                "ORDER BY m.instrument_uid",
                (sector_uid, trading_date, version),
            ).scalars()
            return tuple(str(value) for value in rows)

    def record_sector_membership_source(
        self,
        membership_version_uid: str,
        membership_kind: str,
        block_name: str,
        block_type: int,
        block_version_uid: str,
    ) -> None:
        if membership_kind not in {"ConceptMembership", "ThemeMembership"}:
            raise ValueError("only TDX Concept and Theme memberships are sector subjects")

        def command(transaction: TransactionContext) -> None:
            existing = transaction.connection.exec_driver_sql(
                "SELECT block_version_uid,membership_kind,block_name,block_type "
                "FROM tdx_sector_membership_source WHERE membership_version_uid=?",
                (membership_version_uid,),
            ).one_or_none()
            values = (block_version_uid, membership_kind, block_name, block_type)
            if existing is not None:
                if tuple(existing) != values:
                    raise ValueError("TDX sector membership source is immutable")
                return
            transaction.connection.exec_driver_sql(
                "INSERT INTO tdx_sector_membership_source"
                "(membership_version_uid,block_version_uid,membership_kind,block_name,block_type) "
                "VALUES (?,?,?,?,?)",
                (membership_version_uid, *values),
            )

        self._writer.submit(command).result()

    def add_context_mapping_version(
        self,
        sector_uid: str,
        *,
        etf_instrument_uid: str | None,
        style_instrument_uid: str | None,
        owner_approval_ref: str,
        source_artifact_sha256: str,
        valid_from: datetime,
    ) -> SectorContextMappingVersion:
        if not sector_uid or not (etf_instrument_uid or style_instrument_uid):
            raise ValueError("context mapping requires a sector and at least one context")
        if not owner_approval_ref.strip():
            raise ValueError("context mapping requires an Owner approval reference")
        if len(source_artifact_sha256) != 64:
            raise ValueError("context mapping source artifact hash is invalid")
        instant = format_rfc3339(valid_from)
        with self._runtime.read_connection() as connection:
            if (
                connection.exec_driver_sql(
                    "SELECT 1 FROM sector WHERE sector_uid=?", (sector_uid,)
                ).one_or_none()
                is None
            ):
                raise ReferenceNotFoundError(sector_uid)
            for instrument_uid in (etf_instrument_uid, style_instrument_uid):
                if (
                    instrument_uid is not None
                    and connection.exec_driver_sql(
                        "SELECT 1 FROM instrument WHERE instrument_uid=?", (instrument_uid,)
                    ).one_or_none()
                    is None
                ):
                    raise ReferenceNotFoundError(instrument_uid)
            if (
                connection.exec_driver_sql(
                    "SELECT 1 FROM artifact_object WHERE sha256=?", (source_artifact_sha256,)
                ).one_or_none()
                is None
            ):
                raise ReferenceNotFoundError(source_artifact_sha256)
        mapping_version_uid = new_uid()
        self._write(
            "INSERT INTO sector_context_mapping_version("
            "mapping_version_uid,sector_uid,etf_instrument_uid,style_instrument_uid,"
            "owner_approval_ref,source_artifact_sha256,valid_from,valid_until,created_at)"
            " VALUES (?,?,?,?,?,?,?,NULL,?)",
            (
                mapping_version_uid,
                sector_uid,
                etf_instrument_uid,
                style_instrument_uid,
                owner_approval_ref,
                source_artifact_sha256,
                instant,
                instant,
            ),
        )
        return SectorContextMappingVersion(
            mapping_version_uid,
            sector_uid,
            etf_instrument_uid,
            style_instrument_uid,
            owner_approval_ref,
            source_artifact_sha256,
            instant,
        )

    def _add_mapping(
        self,
        provider: str,
        code: str,
        kind: str,
        instrument: str | None,
        sector: str | None,
        status: str,
        valid_from: datetime,
    ) -> str:
        uid = new_uid()
        self._write(
            "INSERT INTO provider_mapping"
            "(mapping_uid,provider_key,external_code,entity_kind,instrument_uid,sector_uid,"
            "mapping_status,valid_from,valid_until) VALUES (?,?,?,?,?,?,?,?,NULL)",
            (uid, provider, code, kind, instrument, sector, status, format_rfc3339(valid_from)),
        )
        return uid

    def _write(self, sql: str, parameters: tuple[object, ...]) -> None:
        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(sql, parameters)

        self._writer.submit(command).result()


def _listing_effective_at(record: ProviderInstrumentRegistration, instant: str) -> str | None:
    if record.listing_effective_at is not None:
        return format_rfc3339(record.listing_effective_at)
    return None


def _validate_artifact_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("listing Reference artifact hash is invalid")


def _validate_listing_provenance(record: ProviderInstrumentRegistration) -> None:
    has_hash = record.listing_evidence_sha256 is not None
    has_source = record.listing_source_ref is not None
    if has_hash != has_source:
        raise ValueError("listing evidence hash and source reference must be supplied together")
    if has_hash:
        assert record.listing_evidence_sha256 is not None
        _validate_artifact_digest(record.listing_evidence_sha256)
        if record.listing_effective_at is None:
            raise ValueError("listing evidence requires an effective time")
        if not record.listing_source_ref or not record.listing_source_ref.strip():
            raise ValueError("listing evidence requires a source reference")


def _identity_update_required(
    current: _ProviderIdentityRow, record: ProviderInstrumentRegistration, instant: str
) -> bool:
    if record.listing_status == "DISCOVERED" and current.listing_status != "DISCOVERED":
        return False
    return (
        current.exchange,
        current.trading_code,
        current.name,
        current.listing_status,
        current.trading_status,
        current.listing_effective_at,
        current.listing_evidence_sha256,
        current.listing_source_ref,
    ) != (
        record.exchange,
        record.trading_code,
        record.name,
        record.listing_status,
        record.trading_status,
        _listing_effective_at(record, instant),
        record.listing_evidence_sha256,
        record.listing_source_ref,
    )
