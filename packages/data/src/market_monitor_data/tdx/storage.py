from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import (
    format_rfc3339,
    new_uid,
    parse_rfc3339,
    to_scaled_integer,
)
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_data.tdx.models import (
    TdxBar,
    TdxBlockArtifact,
    TdxBlockMembership,
    TdxInstrument,
    TdxQuoteDetail,
)


@dataclass(frozen=True)
class TdxHistoricalCoverage:
    minute_complete: frozenset[tuple[int, str]]
    daily_complete: frozenset[tuple[int, str]]

    @property
    def complete(self) -> frozenset[tuple[int, str]]:
        return self.minute_complete & self.daily_complete


class TdxStorage:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def record_quarantine(
        self,
        epoch_uid: str,
        market: int,
        code: str,
        reason: str,
        observed_at: datetime,
        retry_after: datetime,
    ) -> None:
        _identity(market, code)
        observed = format_rfc3339(observed_at)
        retry = format_rfc3339(retry_after)

        def command(transaction: TransactionContext) -> None:
            row = transaction.connection.exec_driver_sql(
                "SELECT failure_count FROM tdx_symbol_quarantine "
                "WHERE epoch_uid=? AND market=? AND external_code=?",
                (epoch_uid, market, code),
            ).one_or_none()
            if row is None:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO tdx_symbol_quarantine "
                    "(epoch_uid,market,external_code,failure_reason,failure_count,last_failure_at,"
                    "last_success_at,retry_after) VALUES (?,?,?,?,?, ?,NULL,?)",
                    (epoch_uid, market, code, reason, 1, observed, retry),
                )
                return
            transaction.connection.exec_driver_sql(
                "UPDATE tdx_symbol_quarantine SET failure_reason=?,failure_count=?,"
                "last_failure_at=?,retry_after=? WHERE epoch_uid=? "
                "AND market=? AND external_code=?",
                (
                    reason,
                    int(row.failure_count) + 1,
                    observed,
                    retry,
                    epoch_uid,
                    market,
                    code,
                ),
            )

        self._writer.submit(command).result()

    def clear_quarantine(
        self, epoch_uid: str, market: int, code: str, observed_at: datetime
    ) -> None:
        _identity(market, code)
        observed = format_rfc3339(observed_at)

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "UPDATE tdx_symbol_quarantine SET last_success_at=?,retry_after=? "
                "WHERE epoch_uid=? AND market=? AND external_code=?",
                (observed, observed, epoch_uid, market, code),
            )

        self._writer.submit(command).result()

    def active_quarantine(self, epoch_uid: str, observed_at: datetime) -> set[tuple[int, str]]:
        instant = format_rfc3339(observed_at)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT market,external_code FROM tdx_symbol_quarantine "
                "WHERE epoch_uid=? AND retry_after>? ORDER BY market,external_code",
                (epoch_uid, instant),
            ).all()
        return {(int(row.market), str(row.external_code)) for row in rows}

    def quarantine_failure_count(self, epoch_uid: str, market: int, code: str) -> int:
        with self._runtime.read_connection() as connection:
            value = connection.exec_driver_sql(
                "SELECT failure_count FROM tdx_symbol_quarantine "
                "WHERE epoch_uid=? AND market=? AND external_code=?",
                (epoch_uid, market, code),
            ).scalar_one()
        return int(value)

    def record_quote_details(self, entries: Sequence[tuple[str, TdxQuoteDetail]]) -> None:
        if not entries:
            return
        values = [(quote_uid, *_detail_values(detail)) for quote_uid, detail in entries]

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO tdx_quote_detail "
                "(quote_uid,pre_close_scaled,open_scaled,high_scaled,low_scaled,price_scale,"
                "amount_scaled,amount_scale,quote_status,server_time_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(quote_uid) DO NOTHING",
                values,
            )

        self._writer.submit(command).result()

    def record_quote_detail(self, quote_uid: str, detail: TdxQuoteDetail) -> None:
        values = _detail_values(detail)

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO tdx_quote_detail "
                "(quote_uid,pre_close_scaled,open_scaled,high_scaled,low_scaled,price_scale,"
                "amount_scaled,amount_scale,quote_status,server_time_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(quote_uid) DO NOTHING",
                (quote_uid, *values),
            )

        self._writer.submit(command).result()

    def quote_detail(self, quote_uid: str) -> TdxQuoteDetail:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT pre_close_scaled,open_scaled,high_scaled,low_scaled,price_scale,"
                "amount_scaled,amount_scale,quote_status,server_time_raw "
                "FROM tdx_quote_detail WHERE quote_uid=?",
                (quote_uid,),
            ).one()
        return TdxQuoteDetail(
            pre_close=_scaled(row.pre_close_scaled, int(row.price_scale)),
            open=_scaled(row.open_scaled, int(row.price_scale)),
            high=_scaled(row.high_scaled, int(row.price_scale)),
            low=_scaled(row.low_scaled, int(row.price_scale)),
            amount=_scaled(row.amount_scaled, int(row.amount_scale)),
            quote_status=str(row.quote_status),
            server_time_raw=int(row.server_time_raw),
        )

    def record_bars(self, epoch_uid: str, bars: Sequence[TdxBar]) -> int:
        def command(transaction: TransactionContext) -> int:
            stored = 0
            for bar in bars:
                instant = format_rfc3339(bar.timestamp)
                amount_scaled, amount_scale = _amount_storage_values(bar.amount)
                mapping = transaction.connection.exec_driver_sql(
                    "SELECT mapping.instrument_uid FROM provider_mapping mapping "
                    "JOIN market_source_epoch epoch ON epoch.provider_key=mapping.provider_key "
                    "WHERE epoch.epoch_uid=? AND mapping.external_code=? "
                    "AND mapping.entity_kind='INSTRUMENT' AND mapping.mapping_status='RESOLVED' "
                    "AND mapping.valid_from<=epoch.started_at "
                    "AND (mapping.valid_until IS NULL OR mapping.valid_until>epoch.started_at) "
                    "ORDER BY mapping.valid_from DESC LIMIT 1",
                    (epoch_uid, f"{bar.instrument.market}:{bar.instrument.code}"),
                ).scalar_one_or_none()
                if mapping is None:
                    raise ValueError(
                        f"no TDX mapping for {bar.instrument.market}:{bar.instrument.code}"
                    )
                cursor = transaction.connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO tdx_bar "
                    "(bar_uid,epoch_uid,instrument_uid,interval_kind,source_time,open_scaled,"
                    "high_scaled,low_scaled,close_scaled,price_scale,volume,volume_unit,"
                    "amount_scaled,amount_scale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_uid(),
                        epoch_uid,
                        str(mapping),
                        bar.interval,
                        instant,
                        to_scaled_integer(bar.open, 4),
                        to_scaled_integer(bar.high, 4),
                        to_scaled_integer(bar.low, 4),
                        to_scaled_integer(bar.close, 4),
                        4,
                        bar.volume,
                        bar.volume_unit,
                        amount_scaled,
                        amount_scale,
                    ),
                )
                stored += int(cursor.rowcount)
            return stored

        return self._writer.submit(command).result()

    def bars(self, epoch_uid: str, instrument: TdxInstrument, interval: str) -> tuple[TdxBar, ...]:
        """Return this epoch's canonical bars in source-time order."""
        external_code = f"{instrument.market}:{instrument.code}"
        with self._runtime.read_connection() as connection:
            instrument_uid = connection.exec_driver_sql(
                "SELECT mapping.instrument_uid FROM provider_mapping mapping "
                "JOIN market_source_epoch epoch ON epoch.provider_key=mapping.provider_key "
                "WHERE epoch.epoch_uid=? AND mapping.external_code=? "
                "AND mapping.entity_kind='INSTRUMENT' AND mapping.mapping_status='RESOLVED' "
                "ORDER BY mapping.valid_from DESC LIMIT 1",
                (epoch_uid, external_code),
            ).scalar_one_or_none()
            if instrument_uid is None:
                return ()
            rows = connection.exec_driver_sql(
                "SELECT source_time,open_scaled,high_scaled,low_scaled,close_scaled,price_scale,"
                "volume,volume_unit,amount_scaled,amount_scale FROM tdx_bar "
                "WHERE epoch_uid=? AND instrument_uid=? AND interval_kind=? ORDER BY source_time",
                (epoch_uid, instrument_uid, interval),
            ).all()
        return tuple(
            TdxBar(
                instrument,
                interval,
                parse_rfc3339(str(row.source_time)),
                Decimal(int(row.open_scaled)).scaleb(-int(row.price_scale)),
                Decimal(int(row.high_scaled)).scaleb(-int(row.price_scale)),
                Decimal(int(row.low_scaled)).scaleb(-int(row.price_scale)),
                Decimal(int(row.close_scaled)).scaleb(-int(row.price_scale)),
                int(row.volume),
                str(row.volume_unit),
                Decimal(int(row.amount_scaled)).scaleb(-int(row.amount_scale)),
            )
            for row in rows
        )

    def historical_coverage(
        self,
        epoch_uid: str,
        instruments: Sequence[TdxInstrument],
        minute_requirements: Mapping[int, Sequence[datetime]],
        daily_requirements: Mapping[int, Sequence[str]],
    ) -> TdxHistoricalCoverage:
        """Check exact CR-004 windows with bounded aggregate SQL, never per-symbol loads."""
        expected = {(item.market, item.code) for item in instruments}
        if len(expected) != len(instruments):
            raise ValueError("historical coverage instruments must be unique")
        minute_complete: set[tuple[int, str]] = set()
        daily_complete: set[tuple[int, str]] = set()
        with self._runtime.read_connection() as connection:
            for market in sorted({item.market for item in instruments}):
                minute_values = tuple(
                    sorted({format_rfc3339(value) for value in minute_requirements.get(market, ())})
                )
                daily_values = tuple(sorted(set(daily_requirements.get(market, ()))))
                if not minute_values or not daily_values:
                    continue
                minute_slots = ",".join("?" for _ in minute_values)
                daily_slots = ",".join("?" for _ in daily_values)
                rows = connection.exec_driver_sql(
                    "WITH ranked_mapping AS ("
                    "SELECT m.external_code,m.instrument_uid,"
                    "row_number() OVER (PARTITION BY m.external_code "
                    "ORDER BY m.valid_from DESC) AS mapping_rank "
                    "FROM provider_mapping m JOIN market_source_epoch e "
                    "ON e.provider_key=m.provider_key WHERE e.epoch_uid=? "
                    "AND m.entity_kind='INSTRUMENT' AND m.mapping_status='RESOLVED' "
                    "AND substr(m.external_code,1,2)=? AND m.valid_from<=e.started_at "
                    "AND (m.valid_until IS NULL OR m.valid_until>e.started_at)"
                    "), current_mapping AS ("
                    "SELECT external_code,instrument_uid FROM ranked_mapping WHERE mapping_rank=1"
                    "), minute_counts AS ("
                    "SELECT b.instrument_uid,count(DISTINCT b.source_time) AS covered "
                    "FROM current_mapping m JOIN tdx_bar b ON b.instrument_uid=m.instrument_uid "
                    f"WHERE b.epoch_uid=? AND b.interval_kind='1m' "
                    f"AND b.source_time IN ({minute_slots}) "
                    "GROUP BY b.instrument_uid"
                    "), daily_counts AS ("
                    "SELECT b.instrument_uid,count(DISTINCT substr(b.source_time,1,10)) AS covered "
                    "FROM current_mapping m JOIN tdx_bar b ON b.instrument_uid=m.instrument_uid "
                    f"WHERE b.epoch_uid=? AND b.interval_kind='1d' AND b.amount_scaled>0 "
                    f"AND substr(b.source_time,1,10) IN ({daily_slots}) GROUP BY b.instrument_uid"
                    ") SELECT m.external_code,COALESCE(mc.covered,0) AS minute_count,"
                    "COALESCE(dc.covered,0) AS daily_count FROM current_mapping m "
                    "LEFT JOIN minute_counts mc ON mc.instrument_uid=m.instrument_uid "
                    "LEFT JOIN daily_counts dc ON dc.instrument_uid=m.instrument_uid "
                    "ORDER BY m.external_code",
                    (
                        epoch_uid,
                        f"{market}:",
                        epoch_uid,
                        *minute_values,
                        epoch_uid,
                        *daily_values,
                    ),
                ).all()
                for row in rows:
                    external_code = str(row.external_code)
                    identity = (market, external_code.split(":", 1)[1])
                    if identity not in expected:
                        continue
                    if int(row.minute_count) == len(minute_values):
                        minute_complete.add(identity)
                    if int(row.daily_count) == len(daily_values):
                        daily_complete.add(identity)
        return TdxHistoricalCoverage(frozenset(minute_complete), frozenset(daily_complete))

    def start_historical_catchup(
        self,
        epoch_uid: str,
        observed_at: datetime,
        *,
        coverage_checked: int,
        symbols_skipped: int,
        missing_symbols: int,
    ) -> str:
        catchup_uid = new_uid()
        instant = format_rfc3339(observed_at)

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO tdx_historical_catchup_run("
                "catchup_uid,epoch_uid,observed_at,status,coverage_checked,symbols_skipped,"
                "fetch_requests,fetched_bars,inserted_bars,duplicate_bars,missing_symbols,"
                "catchup_duration_ms,created_at,updated_at) "
                "VALUES (?,?,?,'RUNNING',?,?,0,0,0,0,?,0,?,?)",
                (
                    catchup_uid,
                    epoch_uid,
                    instant,
                    coverage_checked,
                    symbols_skipped,
                    missing_symbols,
                    instant,
                    instant,
                ),
            )

        self._writer.submit(command).result()
        return catchup_uid

    def checkpoint_historical_catchup(
        self,
        catchup_uid: str,
        observed_at: datetime,
        *,
        status: str,
        fetch_requests: int,
        fetched_bars: int,
        inserted_bars: int,
        duplicate_bars: int,
        catchup_duration_ms: int,
    ) -> None:
        if status not in {"RUNNING", "FIT", "WARMING_UP", "FAILED"}:
            raise ValueError("invalid historical catch-up status")
        instant = format_rfc3339(observed_at)

        def command(transaction: TransactionContext) -> None:
            cursor = transaction.connection.exec_driver_sql(
                "UPDATE tdx_historical_catchup_run SET status=?,fetch_requests=?,"
                "fetched_bars=?,inserted_bars=?,duplicate_bars=?,catchup_duration_ms=?,"
                "updated_at=? WHERE catchup_uid=?",
                (
                    status,
                    fetch_requests,
                    fetched_bars,
                    inserted_bars,
                    duplicate_bars,
                    catchup_duration_ms,
                    instant,
                    catchup_uid,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(catchup_uid)

        self._writer.submit(command).result()

    def record_block_version(
        self,
        epoch_uid: str,
        artifact: TdxBlockArtifact,
        memberships: Sequence[TdxBlockMembership],
    ) -> str | None:
        _block_artifact(artifact)
        unique_memberships = tuple(
            sorted(
                set(memberships),
                key=lambda item: (item.block_name, item.market, item.code, item.membership_kind),
            )
        )

        def command(transaction: TransactionContext) -> str | None:
            existing = transaction.connection.exec_driver_sql(
                "SELECT block_version_uid FROM tdx_block_artifact_version "
                "WHERE filename=? AND artifact_sha256=?",
                (artifact.filename, artifact.sha256),
            ).scalar_one_or_none()
            if existing is not None:
                return None
            version = (
                int(
                    transaction.connection.exec_driver_sql(
                        "SELECT COALESCE(MAX(version),0) FROM tdx_block_artifact_version "
                        "WHERE filename=?",
                        (artifact.filename,),
                    ).scalar_one()
                )
                + 1
            )
            version_uid = new_uid()
            transaction.connection.exec_driver_sql(
                "INSERT INTO tdx_block_artifact_version "
                "(block_version_uid,epoch_uid,filename,version,source_server,"
                "byte_length,server_hash,artifact_sha256,fetched_at,parser_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    version_uid,
                    epoch_uid,
                    artifact.filename,
                    version,
                    artifact.source_server,
                    artifact.size_bytes,
                    artifact.server_hash,
                    artifact.sha256,
                    format_rfc3339(artifact.fetched_at),
                    artifact.parser_version,
                ),
            )
            for membership in unique_memberships:
                _membership(membership)
                transaction.connection.exec_driver_sql(
                    "INSERT INTO tdx_block_membership "
                    "(block_version_uid,block_name,block_type,membership_kind,"
                    "market,external_code) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        version_uid,
                        membership.block_name,
                        membership.block_type,
                        membership.membership_kind,
                        membership.market,
                        membership.code,
                    ),
                )
            return version_uid

        return self._writer.submit(command).result()

    def block_membership_count(self, version_uid: str) -> int:
        with self._runtime.read_connection() as connection:
            value = connection.exec_driver_sql(
                "SELECT count(*) FROM tdx_block_membership WHERE block_version_uid=?",
                (version_uid,),
            ).scalar_one()
        return int(value)


def _detail_values(detail: TdxQuoteDetail) -> tuple[object, ...]:
    amount_scaled, amount_scale = _amount_storage_values(detail.amount)
    return (
        _optional_scaled(detail.pre_close, 4),
        _optional_scaled(detail.open, 4),
        _optional_scaled(detail.high, 4),
        _optional_scaled(detail.low, 4),
        4,
        amount_scaled,
        amount_scale,
        detail.quote_status,
        detail.server_time_raw,
    )


def _optional_scaled(value: Decimal | None, scale: int) -> int | None:
    return None if value is None else to_scaled_integer(value, scale)


def _amount_storage_values(value: Decimal | None) -> tuple[int | None, int]:
    """Normalize Native TDX float32 amounts to the established canonical scale."""
    if value is None:
        return None, 4
    scale = 4
    scaled = to_scaled_integer(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN), scale)
    if not -(2**63) <= scaled <= 2**63 - 1:
        raise ValueError("TDX amount exceeds SQLite fixed-point range")
    return scaled, scale


def _scaled(value: object, scale: int) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError("stored TDX scaled value must be an integer")
    return Decimal(value).scaleb(-scale)


def _identity(market: int, code: str) -> None:
    if market not in {0, 1} or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValueError("invalid TDX instrument identity")


def _block_artifact(artifact: TdxBlockArtifact) -> None:
    if artifact.filename not in {"block.dat", "block_zs.dat", "block_gn.dat", "block_fg.dat"}:
        raise ValueError("unsupported TDX block artifact")
    if not artifact.source_server or artifact.size_bytes < 0:
        raise ValueError("invalid TDX block artifact metadata")


def _membership(membership: TdxBlockMembership) -> None:
    _identity(membership.market, membership.code)
    if membership.membership_kind not in {
        "IndexMembership",
        "ConceptMembership",
        "ThemeMembership",
        "StyleFactorMembership",
        "StatusMembership",
        "TDXCuratedMembership",
    }:
        raise ValueError("unsupported TDX block membership kind")
