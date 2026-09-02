"""CR-004 fixtures built on the real migrated SQLite runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from market_monitor_analysis.snapshots import PrimaryQuoteStatus, SnapshotBuilder
from market_monitor_data.clock import TradingClock
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import (
    TdxBar,
    TdxBlockArtifact,
    TdxBlockMembership,
    TdxInstrument,
    TdxQuoteDetail,
)
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

from tests.m3.conftest import m3_runtime

AS_OF = datetime(2026, 8, 24, 1, 45, tzinfo=UTC)
_TRADING_DATE = "2026-08-24"
_SAME_CLOCK_DATES = ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21")
_CODES = ("600000", "600001", "600002", "600003", "600004")


@pytest.fixture
def sealed_metric_snapshot(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> Callable[[str], str]:
    """Create one sealed sector snapshot with only canonical CR-004 inputs."""

    runtime, writer, artifacts = m3_runtime

    def build(
        disposition: str = "SHADOW",
        *,
        complete_capabilities: bool = True,
        same_clock_dates: tuple[str, ...] = _SAME_CLOCK_DATES,
    ) -> str:
        references = ReferenceRepository(runtime, writer)
        clock = TradingClock(runtime, writer)
        _import_calendar(clock)
        valid_from = AS_OF - timedelta(days=60)
        instruments: list[str] = []
        identities: list[str] = []
        for code in _CODES:
            instrument_uid = references.create_instrument("STOCK", valid_from)
            identities.append(
                references.add_instrument_identity_version(
                    instrument_uid, "SSE", code, code, "LISTED", "TRADING", valid_from
                )
            )
            references.map_instrument("NATIVE_TDX", f"1:{code}", instrument_uid, valid_from)
            instruments.append(instrument_uid)

        epoch_uid = SourceEpochService(runtime, writer).start_epoch(
            "NATIVE_TDX", "cr004-metric-runner-fixture", AS_OF
        )
        health = CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 60, 120))
        for capability in (
            ("QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS")
            if complete_capabilities
            else ("QUOTES",)
        ):
            health.record(epoch_uid, capability, 1_000_000, 1, AS_OF)
        ingestion = IngestionService(runtime, writer, artifacts)
        ingestion.ingest(
            epoch_uid,
            ProviderBatch(
                "cr004-metric-runner-fixture",
                "2026-08-24T01:44:30.000000Z",
                tuple(
                    ProviderRecord(
                        f"1:{code}",
                        "2026-08-24T01:44:00.000000Z",
                        str(Decimal("10") + Decimal(index) / Decimal("100")),
                        100,
                        {},
                    )
                    for index, code in enumerate(_CODES)
                ),
            ),
        )
        with runtime.read_connection() as connection:
            quote_rows = connection.exec_driver_sql(
                "SELECT q.quote_uid,l.instrument_uid FROM market_quote q "
                "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
                "WHERE l.epoch_uid=? ORDER BY l.instrument_uid",
                (epoch_uid,),
            ).all()
        quote_by_instrument = {str(row.instrument_uid): str(row.quote_uid) for row in quote_rows}
        storage = TdxStorage(runtime, writer)
        storage.record_quote_details(
            tuple(
                (
                    quote_by_instrument[instrument_uid],
                    TdxQuoteDetail(
                        pre_close=Decimal("10"),
                        open=Decimal("10"),
                        high=Decimal("10.5"),
                        low=Decimal("9.5"),
                        amount=Decimal("1000"),
                        quote_status="VALUE",
                        server_time_raw=94400,
                    ),
                )
                for instrument_uid in instruments
            )
        )
        bars: list[TdxBar] = []
        for code in _CODES:
            instrument = TdxInstrument(1, code, "STOCK")
            bars.extend(_minute_bars(instrument))
            bars.extend(_historical_same_clock_bars(instrument))
            bars.extend(_daily_bars(instrument))
        storage.record_bars(epoch_uid, tuple(bars))

        artifact = artifacts.put_bytes(
            b"cr004 metric runner block fixture", "application/vnd.tdx.block"
        )
        artifacts.register(artifact)
        block_version_uid = storage.record_block_version(
            epoch_uid,
            TdxBlockArtifact(
                "block_gn.dat",
                "fixture",
                artifact.size_bytes,
                None,
                artifact.sha256,
                AS_OF,
                "cr004-test",
            ),
            tuple(
                TdxBlockMembership("Metric Fixture", 1, "ThemeMembership", 1, code)
                for code in _CODES
            ),
        )
        assert block_version_uid is not None
        sector_uid = references.create_sector("CONCEPT", valid_from)
        sector_version_uid = references.add_sector_version(sector_uid, "Metric Fixture", valid_from)
        membership_uid = references.freeze_membership(sector_uid, _TRADING_DATE, instruments, AS_OF)
        references.record_sector_membership_source(
            membership_uid,
            "ThemeMembership",
            "Metric Fixture",
            1,
            block_version_uid,
        )
        subject_uid = references.ensure_analysis_subject("SECTOR", sector_uid)

        with runtime.read_connection() as connection:
            bar_uids = [
                str(row.bar_uid)
                for row in connection.exec_driver_sql(
                    "SELECT bar_uid FROM tdx_bar WHERE epoch_uid=? ORDER BY bar_uid", (epoch_uid,)
                ).all()
            ]
        snapshots = SnapshotBuilder(runtime, writer, artifacts)
        manifest_uid = snapshots.create_manifest(
            [quote_by_instrument[instrument_uid] for instrument_uid in instruments],
            AS_OF,
            bar_uids=bar_uids,
            primary_quote_statuses=tuple(
                PrimaryQuoteStatus(instrument_uid, quote_by_instrument[instrument_uid], "VALID")
                for instrument_uid in instruments
            ),
            historical_windows={"turnover_same_clock": same_clock_dates},
        )
        bundle_uid = snapshots.create_reference_bundle(
            [
                ("INSTRUMENT", instruments[0], identities[0]),
                ("SECTOR", sector_uid, sector_version_uid),
                ("MEMBERSHIP", sector_uid, membership_uid),
                ("CALENDAR", "SSE", _TRADING_DATE),
            ]
        )
        snapshot_uid = snapshots.create_snapshot(
            subject_uid,
            manifest_uid,
            bundle_uid,
            disposition,
            (
                ["QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"]
                if complete_capabilities
                else ["QUOTES"]
            ),
            [],
            max_skew_ms=2_000_000_000,
        )
        snapshots.seal(snapshot_uid)
        return snapshot_uid

    return build


def _import_calendar(clock: TradingClock) -> None:
    for trading_date in (*_SAME_CLOCK_DATES, _TRADING_DATE):
        clock.import_day(
            "SSE",
            trading_date,
            "Asia/Shanghai",
            [
                ("CONTINUOUS_AM", f"{trading_date}T01:30:00Z", f"{trading_date}T03:30:00Z"),
                ("CONTINUOUS_PM", f"{trading_date}T05:00:00Z", f"{trading_date}T07:00:00Z"),
            ],
        )


def _minute_bars(instrument: TdxInstrument) -> tuple[TdxBar, ...]:
    return tuple(
        _bar(instrument, AS_OF - timedelta(minutes=offset), "1m", Decimal("100"))
        for offset in range(15, 0, -1)
    )


def _historical_same_clock_bars(instrument: TdxInstrument) -> tuple[TdxBar, ...]:
    bars: list[TdxBar] = []
    for index, trading_date in enumerate(_SAME_CLOCK_DATES):
        start = datetime.fromisoformat(f"{trading_date}T01:40:00+00:00")
        bars.extend(
            _bar(instrument, start + timedelta(minutes=offset), "1m", Decimal("100") + index)
            for offset in range(5)
        )
    return tuple(bars)


def _daily_bars(instrument: TdxInstrument) -> tuple[TdxBar, ...]:
    return tuple(
        _bar(
            instrument,
            AS_OF - timedelta(days=offset),
            "1d",
            Decimal("1000") + offset,
        )
        for offset in range(1, 21)
    )


def _bar(instrument: TdxInstrument, timestamp: datetime, interval: str, amount: Decimal) -> TdxBar:
    return TdxBar(
        instrument,
        interval,
        timestamp,
        Decimal("10"),
        Decimal("10.5"),
        Decimal("9.5"),
        Decimal("10"),
        100,
        "SHARES",
        amount,
    )


__all__ = ["m3_runtime"]
