from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from market_monitor_data.clock import TradingClock
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import (
    ProviderBatch,
    ProviderInstrumentRegistration,
    ProviderRecord,
)
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import (
    TdxBar,
    TdxBlockDownload,
    TdxInstrument,
    TdxRawBar,
    TdxRawQuote,
    TdxSecurity,
)
from market_monitor_data.tdx.provider import NativeTdxProvider
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_data.tdx.transport import TdxTransportError
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


@pytest.fixture
def provider_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DatabaseRuntime, WriterQueue, ArtifactStore]]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        yield runtime, writer, ArtifactStore(runtime, writer)
    finally:
        writer.close()
        runtime.close()


class _Gateway:
    def __init__(self, securities: tuple[TdxSecurity, ...], bad_code: str) -> None:
        self._securities = securities
        self._bad_code = bad_code
        self.quote_calls: list[tuple[TdxInstrument, ...]] = []

    def security_directory(self) -> tuple[TdxSecurity, ...]:
        return self._securities

    def quotes(self, instruments: tuple[TdxInstrument, ...]) -> tuple[TdxRawQuote, ...]:
        self.quote_calls.append(instruments)
        if any(item.code == self._bad_code for item in instruments):
            if len(instruments) == 1:
                raise RuntimeError("bad source symbol")
            return tuple(_raw(item) for item in instruments if item.code != self._bad_code)
        return tuple(_raw(item) for item in instruments)

    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]:
        del instrument, interval, start, count
        raise AssertionError("supporting bars were not requested")

    def block_file(self, filename: str) -> TdxBlockDownload:
        del filename
        raise AssertionError("supporting blocks were not requested")


class _SupportingGateway(_Gateway):
    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]:
        del start, count
        return (
            TdxRawBar(
                instrument.market,
                instrument.code,
                datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.5"),
                500,
                Decimal("5000"),
            ),
        )

    def block_file(self, filename: str) -> TdxBlockDownload:
        code = "000001" if filename == "block_zs.dat" else "600000"
        data = (
            b"\x00" * 384
            + (1).to_bytes(2, "little")
            + "测试".encode("gbk").ljust(9, b"\x00")
            + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little")
            + code.encode("ascii").ljust(7, b"\x00").ljust(2800, b"\x00")
        )
        return TdxBlockDownload(filename, "server-hash", data)


class _UnavailableGateway(_Gateway):
    def quotes(self, instruments: tuple[TdxInstrument, ...]) -> tuple[TdxRawQuote, ...]:
        self.quote_calls.append(instruments)
        raise TdxTransportError("all TDX nodes are unavailable")


def _raw(instrument: TdxInstrument) -> TdxRawQuote:
    return TdxRawQuote(
        market=instrument.market,
        code=instrument.code,
        price=Decimal("10.25"),
        last_close=Decimal("10"),
        open=Decimal("10"),
        high=Decimal("10.5"),
        low=Decimal("9.5"),
        volume_lots=100,
        current_volume_lots=1,
        amount=Decimal("102500"),
        server_time_raw=93000,
    )


def _securities() -> tuple[TdxSecurity, ...]:
    stocks = tuple(
        TdxSecurity(1, f"{600000 + index:06d}", f"Stock {index}", "STOCK") for index in range(76)
    )
    return stocks + (
        TdxSecurity(0, "300001", "ChiNext", "STOCK"),
        TdxSecurity(0, "430001", "BSE excluded", "STOCK"),
        TdxSecurity(1, "000001", "Shanghai Composite", "INDEX"),
        TdxSecurity(1, "000300", "Non-anchor Index", "INDEX"),
        TdxSecurity(0, "159915", "ETF context", "ETF"),
        TdxSecurity(1, "510999", "Non-anchor ETF", "ETF"),
    )


def _seed_listed_reference(
    references: ReferenceRepository,
    artifacts: ArtifactStore,
    securities: tuple[TdxSecurity, ...],
    at: datetime,
) -> None:
    """Install retained Owner-fact fixtures; TDX discovery alone stays untrusted."""
    artifact = artifacts.put_bytes(b'{"fixture":"owner-approved-listing-set"}', "application/json")
    artifacts.register(artifact)
    references.register_provider_instruments(
        "NATIVE_TDX",
        tuple(
            ProviderInstrumentRegistration(
                f"{security.market}:{security.code}",
                security.kind,
                "SSE" if security.market == 1 else "SZSE",
                security.code,
                security.name,
                "LISTED",
                security.trading_status,
                at - timedelta(days=1),
                artifact.sha256,
                "owner-approved-test-fixture",
            )
            for security in securities
            if security.kind == "STOCK"
        ),
        at - timedelta(minutes=1),
    )


def test_sync_uses_shenzhen_shanghai_a_shares_and_keeps_context_separate(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    _seed_listed_reference(references, artifacts, _securities(), now)
    gateway = _Gateway(_securities(), "600074")
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        gateway,
    )

    universe = provider.synchronize_reference(now)

    assert len(universe.primary) == 77
    assert {item.code for item in universe.primary}.isdisjoint({"430001", "000001", "159915"})
    assert {item.code for item in universe.context_indexes} == {"000001"}
    assert {item.code for item in universe.context_etfs} == {"159915"}
    assert references.resolve_provider_mapping("NATIVE_TDX", "1:600000", now) is not None
    assert references.resolve_provider_mapping("NATIVE_TDX", "0:300001", now) is not None
    assert references.resolve_provider_mapping("NATIVE_TDX", "0:430001", now) is not None
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM analysis_subject").scalar_one() == 0


def test_pre_listing_stock_is_registered_but_excluded_from_primary_universe(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Fail if a Native directory record can itself make a stock Primary."""
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        _Gateway(
            (
                TdxSecurity(
                    1,
                    "688810",
                    "Pre-listing fixture",
                    "STOCK",
                ),
            ),
            "never",
        ),
    )

    universe = provider.synchronize_reference(now)

    assert universe.primary == ()
    assert references.resolve_provider_mapping("NATIVE_TDX", "1:688810", now) is not None


def test_listed_stock_enters_primary_only_on_or_after_listing_effective_time(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Fail if a retained Reference fact's effective time is ignored."""
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    fact = artifacts.put_bytes(b'{"fixture":"owner-approved-listing"}', "application/json")
    artifacts.register(fact)
    references.register_provider_instruments(
        "NATIVE_TDX",
        (
            ProviderInstrumentRegistration(
                "1:688811",
                "STOCK",
                "SSE",
                "688811",
                "Listed fixture",
                "LISTED",
                "TRADING",
                now - timedelta(minutes=1),
                fact.sha256,
                "owner-approved-test-fixture",
            ),
            ProviderInstrumentRegistration(
                "1:688812",
                "STOCK",
                "SSE",
                "688812",
                "Future listing fixture",
                "LISTED",
                "TRADING",
                now + timedelta(minutes=1),
                fact.sha256,
                "owner-approved-test-fixture",
            ),
        ),
        now - timedelta(minutes=2),
    )
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        _Gateway(
            (
                TdxSecurity(
                    1,
                    "688811",
                    "Listed fixture",
                    "STOCK",
                ),
                TdxSecurity(
                    1,
                    "688812",
                    "Future listing fixture",
                    "STOCK",
                ),
            ),
            "never",
        ),
    )

    universe = provider.synchronize_reference(now)

    assert {(item.market, item.code) for item in universe.primary} == {(1, "688811")}


def test_unproven_listed_reference_is_excluded_from_primary_universe(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Fail if a LISTED label without a retained fact can make a stock Primary."""
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    references.register_provider_instruments(
        "NATIVE_TDX",
        (
            ProviderInstrumentRegistration(
                "1:688813",
                "STOCK",
                "SSE",
                "688813",
                "Unproven fixture",
                "LISTED",
                "TRADING",
                now - timedelta(minutes=1),
            ),
        ),
        now - timedelta(minutes=2),
    )
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        _Gateway((TdxSecurity(1, "688813", "Unproven fixture", "STOCK"),), "never"),
    )

    assert provider.synchronize_reference(now).primary == ()


def test_directory_discovery_quote_and_bar_never_promote_listing(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Canonical market data cannot manufacture a Reference listing fact."""
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    security = TdxSecurity(1, "688814", "Discovery fixture", "STOCK")
    references = ReferenceRepository(runtime, writer)
    ingestion = IngestionService(runtime, writer, artifacts)
    storage = TdxStorage(runtime, writer)
    provider = NativeTdxProvider(
        runtime,
        references,
        ingestion,
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        storage,
        _Gateway((security,), "never"),
    )

    assert provider.synchronize_reference(now).primary == ()
    epoch_uid = SourceEpochService(runtime, writer).start_epoch(
        "NATIVE_TDX", "listing-boundary", now
    )
    ingestion.ingest(
        epoch_uid,
        ProviderBatch(
            "unlisted-quote-fixture",
            "2026-08-21T01:30:01Z",
            (
                ProviderRecord(
                    "1:688814",
                    "2026-08-21T01:30:00Z",
                    "10.0000",
                    100,
                    {},
                ),
            ),
        ),
    )
    storage.record_bars(
        epoch_uid,
        (
            TdxBar(
                security.instrument,
                "1m",
                now,
                Decimal("10"),
                Decimal("10"),
                Decimal("10"),
                Decimal("10"),
                100,
                "SHARES",
                Decimal("1000"),
            ),
        ),
    )

    assert provider.synchronize_reference(now + timedelta(minutes=1)).primary == ()
    listing = references.provider_listing_states("NATIVE_TDX", now + timedelta(minutes=1))[
        "1:688814"
    ]
    assert listing.listing_status == "DISCOVERED"
    assert listing.listing_evidence_sha256 is None


def test_existing_listed_reference_is_not_downgraded_by_directory_refresh(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Native discovery may observe an identity but cannot replace its listing fact."""
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    security = TdxSecurity(1, "688815", "Listed fixture", "STOCK")
    references = ReferenceRepository(runtime, writer)
    _seed_listed_reference(references, artifacts, (security,), now)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        _Gateway((security,), "never"),
    )

    assert provider.synchronize_reference(now).primary == (security.instrument,)
    listing = references.provider_listing_states("NATIVE_TDX", now)["1:688815"]
    assert listing.listing_status == "LISTED"
    assert listing.listing_effective_at == now - timedelta(days=1)
    assert listing.listing_evidence_sha256 is not None


def test_bulk_reference_registration_is_idempotent_and_keeps_identity_versions(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, _ = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    records = (
        ProviderInstrumentRegistration(
            "1:600000", "STOCK", "SSE", "600000", "Shanghai", "LISTED", "TRADING"
        ),
        ProviderInstrumentRegistration(
            "0:300001",
            "STOCK",
            "SZSE",
            "300001",
            "Shenzhen",
            "LISTED",
            "TRADING",
        ),
    )

    assert references.register_provider_instruments("NATIVE_TDX", records, now) == 2
    assert references.register_provider_instruments("NATIVE_TDX", records, now) == 0
    assert references.resolve_provider_mapping("NATIVE_TDX", "1:600000", now) is not None
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM instrument").scalar_one() == 2
        identity_count = connection.exec_driver_sql(
            "SELECT count(*) FROM instrument_identity_version"
        ).scalar_one()
        assert identity_count == 2


def test_poll_batches_at_75_and_quarantines_only_the_bad_symbol(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    _seed_listed_reference(references, artifacts, _securities(), now)
    gateway = _Gateway(_securities(), "600074")
    storage = TdxStorage(runtime, writer)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        storage,
        gateway,
    )
    provider.synchronize_reference(now)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "tdx-v1", now)

    first = provider.poll_once(epoch_uid, now)

    assert first.requested == 77
    assert first.returned == 76
    assert first.quarantined == 1
    assert max(len(batch) for batch in gateway.quote_calls) == 75
    assert storage.active_quarantine(epoch_uid, now) == {(1, "600074")}
    assert first.health_capabilities["SH_QUOTES"] in {"HEALTHY", "DEGRADED"}
    assert first.health_capabilities["SZ_QUOTES"] == "HEALTHY"
    assert first.health_capabilities["QUOTES"] in {"HEALTHY", "DEGRADED"}

    calls_before_retry = len(gateway.quote_calls)
    second = provider.poll_once(epoch_uid, now + timedelta(seconds=10))

    assert second.requested == 77
    assert all(
        item.code != "600074"
        for batch in gateway.quote_calls[calls_before_retry:]
        for item in batch
    )


def test_poll_uses_validated_tdx_server_time(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Fail if Native TDX falls back to received time when its server clock is valid."""
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    clock = TradingClock(runtime, writer)
    for exchange in ("SSE", "SZSE"):
        clock.import_day(
            exchange,
            "2026-08-21",
            "Asia/Shanghai",
            [
                ("CONTINUOUS_AM", "2026-08-21T01:30:00Z", "2026-08-21T03:30:00Z"),
                ("CONTINUOUS_PM", "2026-08-21T05:00:00Z", "2026-08-21T07:00:00Z"),
            ],
        )
    security = TdxSecurity(1, "600000", "A share", "STOCK")
    references = ReferenceRepository(runtime, writer)
    _seed_listed_reference(references, artifacts, (security,), now)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        _Gateway((security,), "never"),
        clock=clock,
    )
    provider.synchronize_reference(now)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "tdx-clock", now)

    assert provider.poll_once(epoch_uid, now).returned == 1
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT q.source_time,d.server_time_raw FROM market_quote q "
            "JOIN tdx_quote_detail d ON d.quote_uid=q.quote_uid WHERE q.is_current=1"
        ).one()
    assert (str(row.source_time), int(row.server_time_raw)) == (
        "2026-08-21T01:30:00.000000Z",
        93000,
    )
    with runtime.read_connection() as connection:
        quote_health = connection.exec_driver_sql(
            "SELECT health_status,fitness_status,coverage_ppm FROM capability_health_report "
            "WHERE epoch_uid=? AND capability='QUOTES'",
            (epoch_uid,),
        ).one()
    assert (str(quote_health.health_status), str(quote_health.fitness_status)) == ("HEALTHY", "FIT")
    assert int(quote_health.coverage_ppm) == 1_000_000


def test_transport_failure_does_not_quarantine_an_entire_batch(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    _seed_listed_reference(references, artifacts, _securities(), now)
    storage = TdxStorage(runtime, writer)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        storage,
        _UnavailableGateway(_securities(), "600074"),
    )
    provider.synchronize_reference(now)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "tdx-v1", now)

    with pytest.raises(TdxTransportError):
        provider.poll_once(epoch_uid, now)

    assert storage.active_quarantine(epoch_uid, now) == set()


def test_context_bars_and_all_block_files_onboard_concept_theme_sector_subjects(
    provider_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, artifacts = provider_runtime
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    _seed_listed_reference(references, artifacts, _securities(), now)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        _SupportingGateway(_securities(), "never"),
    )
    provider.synchronize_reference(now)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "tdx-v1", now)

    result = provider.refresh_supporting_data(epoch_uid, now, onboard_official_sectors=True)

    assert result.block_versions == 4
    assert result.health_capabilities["INDEX_QUOTES"] == "HEALTHY"
    assert result.health_capabilities["ETF_QUOTES"] == "HEALTHY"
    assert "MINUTE_BARS" not in result.health_capabilities
    assert result.health_capabilities["DAILY_BARS"] == "HEALTHY"
    assert result.health_capabilities["TDX_BLOCKS"] == "HEALTHY"
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM tdx_bar").scalar_one() == 3
        assert connection.exec_driver_sql("SELECT count(*) FROM analysis_subject").scalar_one() == 1
