from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Literal, Protocol

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339

from market_monitor_data.clock import TradingClock
from market_monitor_data.health import CapabilityHealthService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import (
    ProviderBatch,
    ProviderInstrumentRegistration,
    ProviderListingState,
    ProviderRecord,
)
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.blocks import parse_block_file
from market_monitor_data.tdx.models import (
    TdxBar,
    TdxBlockArtifact,
    TdxBlockDownload,
    TdxInstrument,
    TdxQuote,
    TdxQuoteDetail,
    TdxRawBar,
    TdxRawQuote,
    TdxSecurity,
)
from market_monitor_data.tdx.normalization import TdxInvalidRecord, normalize_bar, normalize_quote
from market_monitor_data.tdx.sectors import TdxSectorOnboarding
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_data.tdx.transport import TdxTransportError


class TdxGateway(Protocol):
    def security_directory(self) -> tuple[TdxSecurity, ...]: ...

    def quotes(self, instruments: tuple[TdxInstrument, ...]) -> tuple[TdxRawQuote, ...]: ...

    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]: ...

    def block_file(self, filename: str) -> TdxBlockDownload: ...


@dataclass(frozen=True)
class TdxUniverse:
    primary: tuple[TdxInstrument, ...]
    context_indexes: tuple[TdxInstrument, ...]
    context_etfs: tuple[TdxInstrument, ...]


@dataclass(frozen=True)
class TdxPollResult:
    requested: int
    returned: int
    quarantined: int
    health_capabilities: dict[str, str]
    duration_ms: int


@dataclass(frozen=True)
class TdxSupportingResult:
    block_versions: int
    health_capabilities: dict[str, str]


@dataclass(frozen=True)
class TdxMinuteIncrementResult:
    targets: tuple[tuple[int, datetime], ...]
    request_count: int
    worker_count: int
    expected_count: int
    known_suspended_count: int
    valid_latest_complete_count: int
    missing_or_invalid_count: int
    coverage_ppm: int
    duration_ms: int
    health_capability: str
    failure_counts: dict[str, int]


@dataclass(frozen=True)
class _AcceptedBatch:
    instruments: tuple[TdxInstrument, ...]
    quotes: tuple[TdxQuote, ...]


class NativeTdxProvider:
    PROVIDER_KEY = "NATIVE_TDX"

    def __init__(
        self,
        runtime: DatabaseRuntime,
        references: ReferenceRepository,
        ingestion: IngestionService,
        artifacts: ArtifactStore,
        health: CapabilityHealthService,
        storage: TdxStorage,
        gateway: TdxGateway,
        *,
        clock: TradingClock | None = None,
        minute_gateways: Sequence[TdxGateway] | None = None,
    ) -> None:
        self._runtime = runtime
        self._references = references
        self._ingestion = ingestion
        self._artifacts = artifacts
        self._health = health
        self._storage = storage
        self._gateway = gateway
        self._minute_gateways = tuple(minute_gateways or (gateway,))
        if not self._minute_gateways:
            raise ValueError("at least one minute gateway is required")
        self._clock = clock
        self._universe: TdxUniverse | None = None
        self._previous_quotes: dict[tuple[int, str], TdxQuote] = {}

    def synchronize_reference(self, observed_at: datetime) -> TdxUniverse:
        indexes: list[TdxInstrument] = []
        etfs: list[TdxInstrument] = []
        selected: list[TdxSecurity] = []
        seen: set[tuple[int, str]] = set()
        for security in sorted(
            self._gateway.security_directory(), key=lambda item: (item.market, item.code)
        ):
            identity = (security.market, security.code)
            if identity in seen:
                continue
            seen.add(identity)
            if _is_a_share_stock_candidate(security):
                selected.append(security)
            elif _is_context_index(security):
                selected.append(security)
                indexes.append(security.instrument)
            elif _is_context_etf(security):
                selected.append(security)
                etfs.append(security.instrument)
        self._references.register_provider_instruments(
            self.PROVIDER_KEY,
            tuple(
                ProviderInstrumentRegistration(
                    _external_code(security.instrument),
                    security.kind,
                    "SSE" if security.market == 1 else "SZSE",
                    security.code,
                    security.name,
                    "DISCOVERED",
                    security.trading_status,
                )
                for security in selected
            ),
            observed_at,
        )
        listing_states = self._references.provider_listing_states(self.PROVIDER_KEY, observed_at)
        primary = [
            security.instrument
            for security in selected
            if _is_primary_eligible(
                security, listing_states.get(_external_code(security.instrument)), observed_at
            )
        ]
        self._universe = TdxUniverse(tuple(primary), tuple(indexes), tuple(etfs))
        return self._universe

    def poll_once(self, epoch_uid: str, observed_at: datetime) -> TdxPollResult:
        if self._universe is None:
            raise RuntimeError("synchronize TDX reference before polling")
        started = monotonic()
        requested = len(self._universe.primary)
        quarantined_keys = self._storage.active_quarantine(epoch_uid, observed_at)
        eligible = tuple(
            item
            for item in self._universe.primary
            if (item.market, item.code) not in quarantined_keys
        )
        accepted: list[_AcceptedBatch] = []
        quarantined = 0
        for request_batch in _batches(eligible, 75):
            batch_accepted, batch_quarantined = self._acquire_batch(
                epoch_uid, request_batch, observed_at
            )
            accepted.extend(batch_accepted)
            quarantined += batch_quarantined
        returned = 0
        returned_by_market = {0: 0, 1: 0}
        for index, accepted_batch in enumerate(accepted):
            result = self._ingest_batch(epoch_uid, accepted_batch, observed_at, index)
            returned += result
            for quote in accepted_batch.quotes:
                returned_by_market[quote.instrument.market] += 1
        duration_ms = int((monotonic() - started) * 1000)
        health = self._record_quote_health(epoch_uid, observed_at, returned_by_market, duration_ms)
        return TdxPollResult(requested, returned, quarantined, health, duration_ms)

    def refresh_primary_minute_bars(
        self,
        epoch_uid: str,
        targets: Mapping[int, datetime],
        observed_at: datetime,
        *,
        required_tails: Mapping[int, Sequence[datetime]] | None = None,
    ) -> TdxMinuteIncrementResult:
        """Store exact completed legal minute bars for every non-suspended Primary member."""
        if self._universe is None:
            raise RuntimeError("synchronize TDX reference before refreshing Primary minute bars")
        target_by_market = dict(targets)
        if any(
            target.tzinfo is None or target.utcoffset() is None
            for target in target_by_market.values()
        ):
            raise ValueError("minute targets must be timezone-aware")
        if any(item.market not in target_by_market for item in self._universe.primary):
            raise ValueError("minute targets must cover every Primary market")
        required_by_market = _minute_requirements(target_by_market, required_tails)

        primary_identities = {(item.market, item.code) for item in self._universe.primary}
        suspended = self._known_suspended_primary(observed_at) & primary_identities
        quarantined = self._storage.active_quarantine(epoch_uid, observed_at) & primary_identities
        candidates = tuple(
            item
            for item in self._universe.primary
            if (item.market, item.code) not in suspended
            and (item.market, item.code) not in quarantined
        )
        started = monotonic()
        recent_count = (
            3
            if required_tails is None
            else max(len(values) for values in required_by_market.values()) + 2
        )
        gateways = self._minute_gateways[: min(len(self._minute_gateways), len(candidates))]
        partitions = (
            tuple(tuple(candidates[index :: len(gateways)]) for index in range(len(gateways)))
            if gateways
            else ()
        )
        if len(partitions) <= 1:
            collected = (
                [
                    _collect_primary_minute_bars(
                        gateways[0], partitions[0], required_by_market, recent_count
                    )
                ]
                if partitions
                else []
            )
        else:
            with ThreadPoolExecutor(max_workers=len(partitions)) as executor:
                collected = list(
                    executor.map(
                        lambda item: _collect_primary_minute_bars(
                            item[0], item[1], required_by_market, recent_count
                        ),
                        zip(gateways, partitions, strict=True),
                    )
                )
        bars: list[TdxBar] = []
        failures: Counter[str] = Counter()
        failures["QUARANTINED"] = sum(
            (item.market, item.code) in quarantined and (item.market, item.code) not in suspended
            for item in self._universe.primary
        )
        for collected_bars, collected_failures in collected:
            bars.extend(collected_bars)
            failures.update(collected_failures)
        bars.sort(key=lambda item: (item.instrument.market, item.instrument.code, item.timestamp))
        if bars:
            self._storage.record_bars(epoch_uid, tuple(bars))
        expected_count = len(self._universe.primary)
        tradable_expected_count = expected_count - len(suspended)
        valid_timestamps = {
            (item.instrument.market, item.instrument.code, item.timestamp) for item in bars
        }
        valid_count = sum(
            all(
                (item.market, item.code, timestamp) in valid_timestamps
                for timestamp in required_by_market[item.market]
            )
            for item in candidates
        )
        coverage_ppm = (
            0
            if tradable_expected_count == 0
            else valid_count * 1_000_000 // tradable_expected_count
        )
        duration_ms = int((monotonic() - started) * 1000)
        health_capability = self._record_capability(
            epoch_uid,
            "MINUTE_BARS",
            valid_count,
            tradable_expected_count,
            observed_at,
            duration_ms,
        )
        return TdxMinuteIncrementResult(
            tuple(sorted(target_by_market.items())),
            len(candidates),
            len(gateways),
            expected_count,
            len(suspended),
            valid_count,
            tradable_expected_count - valid_count,
            coverage_ppm,
            duration_ms,
            health_capability,
            dict(sorted(failures.items())),
        )

    def bootstrap_primary_minute_tail(
        self,
        epoch_uid: str,
        targets: Mapping[int, datetime],
        required_tails: Mapping[int, Sequence[datetime]],
        observed_at: datetime,
    ) -> TdxMinuteIncrementResult:
        """Acquire one bounded recent window that covers the required live legal-minute tail."""
        return self.refresh_primary_minute_bars(
            epoch_uid, targets, observed_at, required_tails=required_tails
        )

    def _known_suspended_primary(self, observed_at: datetime) -> set[tuple[int, str]]:
        instant = format_rfc3339(observed_at)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "WITH current_mapping AS ("
                "SELECT m.instrument_uid,m.external_code,"
                "row_number() OVER (PARTITION BY m.external_code "
                "ORDER BY m.valid_from DESC) AS rank "
                "FROM provider_mapping m JOIN instrument i ON i.instrument_uid=m.instrument_uid "
                "WHERE m.provider_key=? AND m.entity_kind='INSTRUMENT' "
                "AND m.mapping_status='RESOLVED' AND i.instrument_kind='STOCK' "
                "AND m.valid_from<=? AND (m.valid_until IS NULL OR m.valid_until>?)"
                ") SELECT external_code FROM current_mapping WHERE rank=1 AND "
                "(SELECT v.trading_status FROM instrument_identity_version v "
                "WHERE v.instrument_uid=current_mapping.instrument_uid AND v.valid_from<=? "
                "AND (v.valid_until IS NULL OR v.valid_until>?) "
                "ORDER BY v.valid_from DESC,v.version DESC LIMIT 1)='SUSPENDED'",
                (self.PROVIDER_KEY, instant, instant, instant, instant),
            ).all()
        return {
            (int(str(row.external_code).split(":", 1)[0]), str(row.external_code).split(":", 1)[1])
            for row in rows
        }

    def refresh_supporting_data(
        self,
        epoch_uid: str,
        observed_at: datetime,
        *,
        onboard_official_sectors: bool = False,
    ) -> TdxSupportingResult:
        if self._universe is None:
            raise RuntimeError("synchronize TDX reference before refreshing supporting data")
        states: dict[str, str] = {}
        states["INDEX_QUOTES"] = self._refresh_context_quotes(
            epoch_uid, self._universe.context_indexes, "INDEX_QUOTES", observed_at, 100
        )
        states["ETF_QUOTES"] = self._refresh_context_quotes(
            epoch_uid, self._universe.context_etfs, "ETF_QUOTES", observed_at, 101
        )
        bar_targets = tuple(
            item
            for item in (
                self._universe.primary[0] if self._universe.primary else None,
                self._universe.context_indexes[0] if self._universe.context_indexes else None,
                self._universe.context_etfs[0] if self._universe.context_etfs else None,
            )
            if item is not None
        )
        states["DAILY_BARS"] = self._refresh_bars(
            epoch_uid, bar_targets, "1d", "DAILY_BARS", observed_at
        )
        block_versions, states["TDX_BLOCKS"] = self._refresh_blocks(epoch_uid, observed_at)
        if onboard_official_sectors:
            TdxSectorOnboarding(self._runtime, self._references).synchronize(epoch_uid, observed_at)
        return TdxSupportingResult(block_versions, states)

    def _acquire_batch(
        self,
        epoch_uid: str,
        instruments: tuple[TdxInstrument, ...],
        observed_at: datetime,
    ) -> tuple[list[_AcceptedBatch], int]:
        try:
            raw_quotes = self._gateway.quotes(instruments)
            expected = {(item.market, item.code) for item in instruments}
            returned = {(item.market, item.code) for item in raw_quotes}
            if len(raw_quotes) != len(instruments) or returned != expected:
                raise ValueError("quote identity/cardinality mismatch")
        except TdxTransportError:
            raise
        except OSError, RuntimeError, ValueError:
            if len(instruments) == 1:
                instrument = instruments[0]
                self._storage.record_quarantine(
                    epoch_uid,
                    instrument.market,
                    instrument.code,
                    "INVALID_RESPONSE",
                    observed_at,
                    observed_at + _retry_delay(),
                )
                return [], 1
            midpoint = len(instruments) // 2
            left, left_quarantined = self._acquire_batch(
                epoch_uid, instruments[:midpoint], observed_at
            )
            right, right_quarantined = self._acquire_batch(
                epoch_uid, instruments[midpoint:], observed_at
            )
            return left + right, left_quarantined + right_quarantined
        by_identity = {(item.market, item.code): item for item in raw_quotes}
        quotes: list[TdxQuote] = []
        quarantined = 0
        for instrument in instruments:
            normalized = normalize_quote(
                by_identity[(instrument.market, instrument.code)],
                instrument,
                observed_at,
                previous=self._previous_quotes.get((instrument.market, instrument.code)),
            )
            if isinstance(normalized, TdxInvalidRecord):
                self._storage.record_quarantine(
                    epoch_uid,
                    instrument.market,
                    instrument.code,
                    normalized.reason,
                    observed_at,
                    observed_at + _retry_delay(),
                )
                quarantined += 1
                continue
            if normalized.quote_status != "VALUE":
                self._storage.record_quarantine(
                    epoch_uid,
                    instrument.market,
                    instrument.code,
                    normalized.quote_status,
                    observed_at,
                    observed_at + _retry_delay(),
                )
                quarantined += 1
                continue
            self._previous_quotes[(instrument.market, instrument.code)] = normalized
            quotes.append(normalized)
        return ([_AcceptedBatch(instruments, tuple(quotes))] if quotes else []), quarantined

    def _ingest_batch(
        self,
        epoch_uid: str,
        batch: _AcceptedBatch,
        observed_at: datetime,
        sequence: int,
    ) -> int:
        records = tuple(
            ProviderRecord(
                _external_code(quote.instrument),
                (
                    self._clock.tdx_quote_time(
                        "SSE" if quote.instrument.market == 1 else "SZSE",
                        quote.received_at,
                        quote.server_time_raw,
                    )
                    if self._clock is not None
                    else None
                ),
                format(quote.price, "f") if quote.price is not None else None,
                quote.volume,
                {
                    "market": quote.instrument.market,
                    "code": quote.instrument.code,
                    "pre_close": _decimal(quote.pre_close),
                    "open": _decimal(quote.open),
                    "high": _decimal(quote.high),
                    "low": _decimal(quote.low),
                    "amount": _decimal(quote.amount),
                    "quote_status": quote.quote_status,
                    "server_time_raw": quote.server_time_raw,
                },
            )
            for quote in batch.quotes
        )
        if not records:
            return 0
        identifier = f"tdx-{format_rfc3339(observed_at)}-{sequence}"
        result = self._ingestion.ingest(
            epoch_uid,
            ProviderBatch(identifier, format_rfc3339(observed_at), records),
        )
        self._store_details(result.batch_uid, batch.quotes)
        return result.quotes

    def _store_details(self, batch_uid: str, quotes: Sequence[TdxQuote]) -> None:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT r.external_code,q.quote_uid FROM raw_market_record r "
                "JOIN market_quote q ON q.batch_uid=r.batch_uid AND q.record_index=r.record_index "
                "WHERE r.batch_uid=? AND q.is_current=1",
                (batch_uid,),
            ).all()
        quote_ids = {str(row.external_code): str(row.quote_uid) for row in rows}
        self._storage.record_quote_details(
            tuple(
                (
                    quote_ids[_external_code(quote.instrument)],
                    TdxQuoteDetail(
                        quote.pre_close,
                        quote.open,
                        quote.high,
                        quote.low,
                        quote.amount,
                        quote.quote_status,
                        quote.server_time_raw,
                    ),
                )
                for quote in quotes
            )
        )

    def _record_quote_health(
        self,
        epoch_uid: str,
        observed_at: datetime,
        returned_by_market: dict[int, int],
        duration_ms: int,
    ) -> dict[str, str]:
        if self._universe is None:
            return {}
        states: dict[str, str] = {}
        for market, capability in ((1, "SH_QUOTES"), (0, "SZ_QUOTES")):
            total = sum(1 for item in self._universe.primary if item.market == market)
            if total == 0:
                continue
            completed = min(returned_by_market[market], total)
            coverage = completed * 1_000_000 // total
            states[capability] = self._health.record(
                epoch_uid, capability, coverage, duration_ms, observed_at
            ).health_status
        total = len(self._universe.primary)
        if total:
            coverage = sum(min(returned_by_market[market], total) for market in (0, 1))
            states["QUOTES"] = self._health.record(
                epoch_uid, "QUOTES", coverage * 1_000_000 // total, duration_ms, observed_at
            ).health_status
        return states

    def _refresh_context_quotes(
        self,
        epoch_uid: str,
        instruments: tuple[TdxInstrument, ...],
        capability: str,
        observed_at: datetime,
        sequence: int,
    ) -> str:
        if not instruments:
            return "UNKNOWN"
        try:
            raw_quotes = self._gateway.quotes(instruments)
        except OSError, RuntimeError, ValueError:
            raw_quotes = ()
        expected = {(item.market, item.code) for item in instruments}
        returned = {(item.market, item.code) for item in raw_quotes}
        quotes: list[TdxQuote] = []
        if len(raw_quotes) == len(instruments) and returned == expected:
            by_identity = {(item.market, item.code): item for item in raw_quotes}
            for instrument in instruments:
                normalized = normalize_quote(
                    by_identity[(instrument.market, instrument.code)], instrument, observed_at
                )
                if isinstance(normalized, TdxInvalidRecord) or normalized.quote_status != "VALUE":
                    continue
                quotes.append(normalized)
        if quotes:
            self._ingest_batch(
                epoch_uid,
                _AcceptedBatch(tuple(quote.instrument for quote in quotes), tuple(quotes)),
                observed_at,
                sequence,
            )
        return self._record_capability(
            epoch_uid, capability, len(quotes), len(instruments), observed_at, 0
        )

    def _refresh_bars(
        self,
        epoch_uid: str,
        instruments: tuple[TdxInstrument, ...],
        interval: Literal["1m", "1d"],
        capability: str,
        observed_at: datetime,
    ) -> str:
        bars: list[TdxBar] = []
        successful = 0
        for instrument in instruments:
            try:
                raw_bars = self._gateway.bars(instrument, interval, count=1)
            except OSError, RuntimeError, ValueError:
                continue
            normalized = [normalize_bar(item, instrument, interval) for item in raw_bars]
            valid = [item for item in normalized if isinstance(item, TdxBar)]
            if valid:
                bars.extend(valid)
                successful += 1
        if bars:
            self._storage.record_bars(epoch_uid, tuple(bars))
        return self._record_capability(
            epoch_uid, capability, successful, len(instruments), observed_at, 0
        )

    def _refresh_blocks(self, epoch_uid: str, observed_at: datetime) -> tuple[int, str]:
        versions = 0
        successful = 0
        for filename in ("block.dat", "block_zs.dat", "block_gn.dat", "block_fg.dat"):
            try:
                download = self._gateway.block_file(filename)
                stored = self._artifacts.put_bytes(download.data, "application/vnd.tdx.block")
                self._artifacts.register(stored)
                version_uid = self._storage.record_block_version(
                    epoch_uid,
                    TdxBlockArtifact(
                        filename,
                        download.source_server or "UNSPECIFIED_TDX_NODE",
                        len(download.data),
                        download.server_hash,
                        stored.sha256,
                        observed_at,
                        "cr002-v1",
                    ),
                    parse_block_file(download.data, filename),
                )
            except OSError, RuntimeError, ValueError:
                continue
            successful += 1
            versions += int(version_uid is not None)
        return versions, self._record_capability(
            epoch_uid, "TDX_BLOCKS", successful, 4, observed_at, 0
        )

    def _record_capability(
        self,
        epoch_uid: str,
        capability: str,
        completed: int,
        total: int,
        observed_at: datetime,
        latency_ms: int,
    ) -> str:
        coverage = 0 if total == 0 else min(completed, total) * 1_000_000 // total
        return self._health.record(
            epoch_uid, capability, coverage, latency_ms, observed_at
        ).health_status


def _collect_primary_minute_bars(
    gateway: TdxGateway,
    instruments: Sequence[TdxInstrument],
    required_by_market: Mapping[int, tuple[datetime, ...]],
    recent_count: int,
) -> tuple[list[TdxBar], Counter[str]]:
    bars: list[TdxBar] = []
    failures: Counter[str] = Counter()
    for instrument in instruments:
        required = frozenset(required_by_market[instrument.market])
        try:
            raw_bars = gateway.bars(instrument, "1m", count=recent_count)
        except (OSError, RuntimeError, ValueError) as error:
            failures[_minute_failure_code(error)] += 1
            continue
        matched: dict[datetime, TdxBar] = {}
        required_invalid = False
        duplicate_required = False
        for raw in raw_bars:
            normalized = normalize_bar(raw, instrument, "1m")
            if isinstance(normalized, TdxInvalidRecord):
                required_invalid = required_invalid or raw.timestamp in required
                continue
            if normalized.timestamp in required:
                duplicate_required = duplicate_required or normalized.timestamp in matched
                matched[normalized.timestamp] = normalized
        if set(matched) == required and not duplicate_required:
            bars.extend(matched.values())
            continue
        if required_invalid or duplicate_required:
            failures["INVALID"] += 1
        elif not raw_bars:
            failures["EMPTY"] += 1
        else:
            failures["NON_TARGET" if len(required) == 1 else "INCOMPLETE_TAIL"] += 1
    return bars, failures


def _minute_requirements(
    targets: Mapping[int, datetime], required_tails: Mapping[int, Sequence[datetime]] | None
) -> dict[int, tuple[datetime, ...]]:
    if required_tails is None:
        return {market: (target,) for market, target in targets.items()}
    if set(required_tails) != set(targets):
        raise ValueError("minute tails must cover exactly the target markets")
    requirements: dict[int, tuple[datetime, ...]] = {}
    for market, target in targets.items():
        values = tuple(required_tails[market])
        if (
            not values
            or values[-1] != target
            or tuple(sorted(set(values))) != values
            or any(value.tzinfo is None or value.utcoffset() is None for value in values)
        ):
            raise ValueError("minute tails must be ordered exact timezone-aware targets")
        requirements[market] = values
    return requirements


def _minute_failure_code(error: Exception) -> str:
    detail = f"{type(error).__name__}: {error}".lower()
    if "throttl" in detail or "rate limit" in detail or "too many request" in detail:
        return "THROTTLED"
    if "timeout" in detail or "timed out" in detail:
        return "TIMEOUT"
    if any(
        value in detail
        for value in (
            "connection reset",
            "connection aborted",
            "connection refused",
            "broken pipe",
            "network is unreachable",
            "closed connection",
            "eof",
        )
    ):
        return "DISCONNECT"
    if any(value in detail for value in ("protocol", "parse", "malformed", "decode", "truncated")):
        return "PARSE_ERROR"
    return "TRANSPORT"


def _batches(items: Sequence[TdxInstrument], size: int) -> tuple[tuple[TdxInstrument, ...], ...]:
    return tuple(tuple(items[index : index + size]) for index in range(0, len(items), size))


def _is_a_share_stock_candidate(security: TdxSecurity) -> bool:
    if security.kind != "STOCK":
        return False
    if security.market == 1:
        return security.code[:3] in {"600", "601", "603", "605", "688"}
    return security.code[:3] in {"000", "001", "002", "003", "300", "301"}


def _is_primary_eligible(
    security: TdxSecurity,
    listing: ProviderListingState | None,
    observed_at: datetime,
) -> bool:
    """Apply the frozen Primary range after Reference-fact resolution only."""
    return (
        _is_a_share_stock_candidate(security)
        and listing is not None
        and listing.listing_status == "LISTED"
        and listing.listing_effective_at is not None
        and listing.listing_effective_at <= observed_at
        and listing.listing_evidence_sha256 is not None
        and bool(listing.listing_source_ref and listing.listing_source_ref.strip())
    )


# ponytail: P0 keeps only broad anchor context.
# Add an owner-governed universe when more context is needed.
_CONTEXT_INDEXES = frozenset({(1, "000001"), (0, "399001"), (0, "399006")})
_CONTEXT_ETFS = frozenset({(1, "510300"), (0, "159915")})


def _is_context_index(security: TdxSecurity) -> bool:
    return security.kind == "INDEX" and (security.market, security.code) in _CONTEXT_INDEXES


def _is_context_etf(security: TdxSecurity) -> bool:
    return security.kind == "ETF" and (security.market, security.code) in _CONTEXT_ETFS


def _external_code(instrument: TdxInstrument) -> str:
    return f"{instrument.market}:{instrument.code}"


def _decimal(value: object) -> str | None:
    return None if value is None else format(value, "f")


def _retry_delay() -> timedelta:
    return timedelta(minutes=5)
