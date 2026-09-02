from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from market_monitor_data.health import SourceEpochService
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
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


@pytest.fixture
def tdx_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DatabaseRuntime, WriterQueue, ArtifactStore, str, str]]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    artifacts = ArtifactStore(runtime, writer)
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    instrument_uid = references.create_instrument("STOCK", now)
    references.add_instrument_identity_version(
        instrument_uid, "SSE", "600000", "Storage Fixture", "LISTED", "TRADING", now
    )
    references.map_instrument("NATIVE_TDX", "1:600000", instrument_uid, now)
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "tdx-v1", now)
    ingestion = IngestionService(runtime, writer, artifacts)
    result = ingestion.ingest(
        epoch_uid,
        ProviderBatch(
            "tdx-storage-fixture",
            "2026-08-21T01:30:01Z",
            (ProviderRecord("1:600000", "2026-08-21T01:30:00Z", "10.2500", 100, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?", (result.lineages[0],)
            ).scalar_one()
        )
    try:
        yield runtime, writer, artifacts, epoch_uid, quote_uid
    finally:
        writer.close()
        runtime.close()


def test_quarantine_tracks_failure_and_later_retry(
    tdx_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore, str, str],
) -> None:
    runtime, writer, _, epoch_uid, _ = tdx_runtime
    storage = TdxStorage(runtime, writer)
    now = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)

    storage.record_quarantine(
        epoch_uid, 1, "600000", "INVALID_RESPONSE", now, now + timedelta(minutes=5)
    )
    storage.record_quarantine(
        epoch_uid, 1, "600000", "INVALID_RESPONSE", now, now + timedelta(minutes=5)
    )

    assert storage.active_quarantine(epoch_uid, now) == {(1, "600000")}
    assert storage.quarantine_failure_count(epoch_uid, 1, "600000") == 2
    storage.clear_quarantine(epoch_uid, 1, "600000", now + timedelta(minutes=6))
    assert storage.active_quarantine(epoch_uid, now + timedelta(minutes=6)) == set()


def test_quote_detail_batch_persists_entries_idempotently(
    tdx_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore, str, str],
) -> None:
    runtime, writer, _, _, quote_uid = tdx_runtime
    detail = TdxQuoteDetail(
        pre_close=Decimal("10"),
        open=Decimal("10.10"),
        high=Decimal("10.30"),
        low=Decimal("9.90"),
        amount=Decimal("1260.50"),
        quote_status="VALUE",
        server_time_raw=93000,
    )

    storage = TdxStorage(runtime, writer)
    storage.record_quote_details(((quote_uid, detail), (quote_uid, detail)))

    assert storage.quote_detail(quote_uid) == detail


def test_quote_bars_and_block_versions_use_fixed_point_storage(
    tdx_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore, str, str],
) -> None:
    runtime, writer, artifacts, epoch_uid, quote_uid = tdx_runtime
    storage = TdxStorage(runtime, writer)
    instrument = TdxInstrument(1, "600000", "STOCK")
    detail = TdxQuoteDetail(
        pre_close=Decimal("10"),
        open=Decimal("10.10"),
        high=Decimal("10.30"),
        low=Decimal("9.90"),
        amount=Decimal("1260.50"),
        quote_status="VALUE",
        server_time_raw=93000,
    )

    storage.record_quote_detail(quote_uid, detail)
    assert storage.quote_detail(quote_uid) == detail
    assert (
        storage.record_bars(
            epoch_uid,
            (
                TdxBar(
                    instrument=instrument,
                    interval="1m",
                    timestamp=datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=500,
                    volume_unit="SHARES",
                    amount=Decimal("5000"),
                ),
            ),
        )
        == 1
    )

    first = artifacts.put_bytes(b"block-v1", "application/octet-stream")
    artifacts.register(first)
    artifact = TdxBlockArtifact(
        filename="block_gn.dat",
        source_server="127.0.0.1:7709",
        size_bytes=len(b"block-v1"),
        server_hash=None,
        sha256=first.sha256,
        fetched_at=datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
        parser_version="cr002-v1",
    )
    membership = TdxBlockMembership("Theme", 1, "ThemeMembership", 1, "600000")

    first_version = storage.record_block_version(epoch_uid, artifact, (membership,))
    assert first_version is not None
    assert storage.record_block_version(epoch_uid, artifact, (membership,)) is None

    second = artifacts.put_bytes(b"block-v2", "application/octet-stream")
    artifacts.register(second)
    changed = TdxBlockArtifact(
        filename=artifact.filename,
        source_server=artifact.source_server,
        size_bytes=len(b"block-v2"),
        server_hash=None,
        sha256=second.sha256,
        fetched_at=artifact.fetched_at + timedelta(minutes=1),
        parser_version=artifact.parser_version,
    )
    second_version = storage.record_block_version(epoch_uid, changed, (membership,))

    assert second_version is not None
    assert second_version != first_version
    assert storage.block_membership_count(second_version) == 1


def test_tdx_storage_normalizes_native_fractional_amount_to_canonical_scale(
    tdx_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore, str, str],
) -> None:
    """Break if a real float32-derived TDX amount bypasses canonical 4-decimal storage."""
    runtime, writer, _, epoch_uid, _ = tdx_runtime
    amount = Decimal("244841.046875")
    storage = TdxStorage(runtime, writer)

    assert (
        storage.record_bars(
            epoch_uid,
            (
                TdxBar(
                    instrument=TdxInstrument(1, "600000", "STOCK"),
                    interval="1m",
                    timestamp=datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=500,
                    volume_unit="SHARES",
                    amount=amount,
                ),
            ),
        )
        == 1
    )
    assert storage.bars(epoch_uid, TdxInstrument(1, "600000", "STOCK"), "1m")[0].amount == Decimal(
        "244841.0469"
    )
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT amount_scale FROM tdx_bar").scalar_one() == 4


def test_tdx_storage_uses_epoch_reference_mapping_for_historical_bars(
    tdx_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore, str, str],
) -> None:
    """Break if a current TDX reference map cannot store its prior trading-day bars."""
    runtime, writer, _, epoch_uid, _ = tdx_runtime

    assert (
        TdxStorage(runtime, writer).record_bars(
            epoch_uid,
            (
                TdxBar(
                    instrument=TdxInstrument(1, "600000", "STOCK"),
                    interval="1d",
                    timestamp=datetime(2026, 8, 20, 7, 0, tzinfo=UTC),
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=500,
                    volume_unit="SHARES",
                    amount=Decimal("5000"),
                ),
            ),
        )
        == 1
    )
