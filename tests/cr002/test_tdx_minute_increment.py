"""Primary-Universe legal-minute increment contracts for Native TDX."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderInstrumentRegistration
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import (
    TdxBlockDownload,
    TdxInstrument,
    TdxRawBar,
    TdxRawQuote,
    TdxSecurity,
)
from market_monitor_data.tdx.provider import NativeTdxProvider
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


@pytest.fixture
def minute_runtime(tmp_path: Path) -> Iterator[tuple[DatabaseRuntime, WriterQueue, ArtifactStore]]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    artifacts = ArtifactStore(runtime, writer)
    try:
        yield runtime, writer, artifacts
    finally:
        writer.close()
        runtime.close()


class _MinuteGateway:
    def __init__(self, target: datetime) -> None:
        self.target = target
        self.bar_calls: list[tuple[TdxInstrument, int]] = []

    def security_directory(self) -> tuple[TdxSecurity, ...]:
        return (
            TdxSecurity(1, "600000", "A", "STOCK"),
            TdxSecurity(1, "600001", "B", "STOCK"),
            TdxSecurity(1, "600002", "C", "STOCK"),
            TdxSecurity(1, "600003", "Paused", "STOCK", trading_status="SUSPENDED"),
        )

    def quotes(self, instruments: tuple[TdxInstrument, ...]) -> tuple[TdxRawQuote, ...]:
        raise AssertionError(f"quotes are not part of a minute increment: {instruments}")

    def bars(
        self, instrument: TdxInstrument, interval: str, *, start: int = 0, count: int = 1
    ) -> tuple[TdxRawBar, ...]:
        del start
        assert interval == "1m"
        self.bar_calls.append((instrument, count))
        if instrument.code == "600002":
            times: tuple[datetime, ...] = (
                self.target - timedelta(minutes=3),
                self.target - timedelta(minutes=2),
            )
        else:
            times = (
                self.target - timedelta(minutes=1),
                self.target,
                self.target + timedelta(minutes=1),
            )
        return tuple(
            TdxRawBar(
                instrument.market,
                instrument.code,
                value,
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.5"),
                100,
                Decimal("1000"),
            )
            for value in times
        )

    def block_file(self, filename: str) -> TdxBlockDownload:
        raise AssertionError(f"blocks are not part of a minute increment: {filename}")


def test_primary_minute_increment_records_only_the_exact_completed_target_and_true_coverage(
    minute_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, artifacts = minute_runtime
    target = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
    gateway = _MinuteGateway(target)
    references = ReferenceRepository(runtime, writer)
    _seed_primary_listing_facts(references, artifacts, gateway.security_directory(), target)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        gateway,
    )
    provider.synchronize_reference(target)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "minute-test", target)

    result = provider.refresh_primary_minute_bars(
        epoch_uid, {1: target}, target + timedelta(seconds=20)
    )

    assert result.request_count == 3
    assert result.worker_count == 1
    assert result.expected_count == 4
    assert result.known_suspended_count == 1
    assert result.valid_latest_complete_count == 2
    assert result.missing_or_invalid_count == 1
    assert result.coverage_ppm == 666_666
    assert result.failure_counts["NON_TARGET"] == 1
    assert all(count == 3 for _, count in gateway.bar_calls)
    assert {instrument.code for instrument, _ in gateway.bar_calls} == {
        "600000",
        "600001",
        "600002",
    }

    with runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "SELECT source_time FROM tdx_bar WHERE epoch_uid=? "
            "AND interval_kind='1m' ORDER BY source_time",
            (epoch_uid,),
        ).all()
        health = connection.exec_driver_sql(
            "SELECT coverage_ppm FROM capability_health_report WHERE epoch_uid=? "
            "AND capability='MINUTE_BARS'",
            (epoch_uid,),
        ).scalar_one()
    assert [str(row.source_time) for row in rows] == ["2026-08-25T01:30:00.000000Z"] * 2
    assert int(health) == 666_666


def test_primary_minute_increment_counts_active_quarantine_against_coverage(
    minute_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """A quarantined Primary symbol is unavailable, not silently removed from coverage."""
    runtime, writer, artifacts = minute_runtime
    target = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
    gateway = _MinuteGateway(target)
    references = ReferenceRepository(runtime, writer)
    _seed_primary_listing_facts(references, artifacts, gateway.security_directory(), target)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        gateway,
    )
    provider.synchronize_reference(target)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "minute-test", target)
    TdxStorage(runtime, writer).record_quarantine(
        epoch_uid,
        1,
        "600001",
        "INVALID_RESPONSE",
        target,
        target + timedelta(minutes=5),
    )

    result = provider.refresh_primary_minute_bars(
        epoch_uid, {1: target}, target + timedelta(seconds=20)
    )

    assert result.request_count == 2
    assert result.valid_latest_complete_count == 1
    assert result.missing_or_invalid_count == 2
    assert result.coverage_ppm == 333_333
    assert result.failure_counts["QUARANTINED"] == 1
    assert {instrument.code for instrument, _ in gateway.bar_calls} == {"600000", "600002"}


def test_primary_minute_increment_ignores_non_primary_suspension_mappings(
    minute_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only suspended identities inside the synchronized Primary Universe affect coverage."""
    runtime, writer, artifacts = minute_runtime
    target = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
    gateway = _MinuteGateway(target)
    references = ReferenceRepository(runtime, writer)
    _seed_primary_listing_facts(references, artifacts, gateway.security_directory(), target)
    provider = NativeTdxProvider(
        runtime,
        references,
        IngestionService(runtime, writer, artifacts),
        artifacts,
        CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 5_000, 60)),
        TdxStorage(runtime, writer),
        gateway,
    )
    provider.synchronize_reference(target)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "minute-test", target)
    monkeypatch.setattr(
        provider,
        "_known_suspended_primary",
        lambda _: {(1, "600003"), (1, "900001")},
    )

    result = provider.refresh_primary_minute_bars(
        epoch_uid, {1: target}, target + timedelta(seconds=20)
    )

    assert result.known_suspended_count == 1
    assert result.missing_or_invalid_count == 1
    assert result.coverage_ppm == 666_666


def _seed_primary_listing_facts(
    references: ReferenceRepository,
    artifacts: ArtifactStore,
    securities: tuple[TdxSecurity, ...],
    at: datetime,
) -> None:
    artifact = artifacts.put_bytes(b'{"fixture":"minute-primary-listing"}', "application/json")
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
                "owner-approved-minute-test-fixture",
            )
            for security in securities
        ),
        at - timedelta(minutes=1),
    )
