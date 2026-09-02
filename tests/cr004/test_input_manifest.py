"""CR-004 input-manifest compatibility and version-2 lineage behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.snapshots import (
    PrimaryQuoteStatus,
    SnapshotBuilder,
    SnapshotError,
    SnapshotFreshnessError,
)
from market_monitor_data.health import CapabilityHealthService, HealthThresholds, SourceEpochService
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import TdxBar, TdxInstrument
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext

AS_OF = datetime(2026, 8, 24, 1, 35, tzinfo=UTC)
QUOTE_TIME = "2026-08-24T01:34:00Z"


def test_valid_primary_status_rejects_an_empty_quote_uid() -> None:
    """Fail if a supposedly valid status can lose its canonical quote identity."""
    with pytest.raises(ValueError, match="quote UID"):
        PrimaryQuoteStatus("instrument", "", "VALID")


def _metric_inputs(m3_runtime: tuple[Any, Any, Any]) -> tuple[str, list[str], list[str], str, str]:
    runtime, writer, artifacts = m3_runtime
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
    instruments: list[str] = []
    identities: list[str] = []
    for code in ("600000", "600001", "600002", "600003"):
        instrument_uid = references.create_instrument("STOCK", now)
        identity_uid = references.add_instrument_identity_version(
            instrument_uid, "SSE", code, f"Fixture {code}", "LISTED", "TRADING", now
        )
        references.map_instrument("NATIVE_TDX", f"1:{code}", instrument_uid, now)
        instruments.append(instrument_uid)
        identities.append(identity_uid)
    subject_uid = references.ensure_analysis_subject("INSTRUMENT", instruments[0])
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "cr004-fixture", now)
    CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 2_000, 120)).record(
        epoch_uid, "QUOTES", 1_000_000, 10, datetime(2026, 8, 24, 1, 34, tzinfo=UTC)
    )
    ingestion = IngestionService(runtime, writer, artifacts).ingest(
        epoch_uid,
        ProviderBatch(
            "cr004-manifest-fixture",
            "2026-08-24T01:34:01Z",
            (ProviderRecord("1:600000", QUOTE_TIME, "10.0000", 100, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?", (ingestion.lineages[0],)
            ).scalar_one()
        )
    storage = TdxStorage(runtime, writer)
    storage.record_bars(
        epoch_uid,
        (
            TdxBar(
                TdxInstrument(1, "600000", "STOCK"),
                "1m",
                datetime(2026, 8, 24, 1, 34, tzinfo=UTC),
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
    with runtime.read_connection() as connection:
        bar_uid = str(
            connection.exec_driver_sql(
                "SELECT bar_uid FROM tdx_bar WHERE epoch_uid=?", (epoch_uid,)
            ).scalar_one()
        )
    return quote_uid, [bar_uid], instruments, subject_uid, identities[0]


def test_v2_manifest_freezes_primary_statuses_bars_and_history_for_snapshot(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if CR-004 drops a Primary status, bar lineage, or window identity."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, bar_uids, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    snapshots = SnapshotBuilder(runtime, writer, artifacts)

    manifest_uid = snapshots.create_manifest(
        [quote_uid],
        AS_OF,
        bar_uids=bar_uids,
        primary_quote_statuses=(
            PrimaryQuoteStatus(instruments[0], quote_uid, "VALID"),
            PrimaryQuoteStatus(instruments[1], None, "MISSING"),
            PrimaryQuoteStatus(instruments[2], None, "SUSPENDED"),
            PrimaryQuoteStatus(instruments[3], None, "NO_VALID_QUOTE"),
        ),
        historical_windows={
            "turnover_same_clock": ("2026-08-17", "2026-08-18", "2026-08-19"),
        },
    )
    document = snapshots.replay_manifest(manifest_uid)
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instruments[0], identity_uid)])
    snapshot_uid = snapshots.create_snapshot(
        subject_uid, manifest_uid, bundle_uid, "SHADOW", ["QUOTES"], [], max_skew_ms=1_000
    )

    assert document["schema_version"] == 2
    assert document["bar_uids"] == bar_uids
    assert document["historical_windows"] == {
        "turnover_same_clock": ["2026-08-17", "2026-08-18", "2026-08-19"],
    }
    assert {
        (entry["instrument_uid"], entry["status"]) for entry in document["primary_quote_statuses"]
    } == {
        (instruments[0], "VALID"),
        (instruments[1], "MISSING"),
        (instruments[2], "SUSPENDED"),
        (instruments[3], "NO_VALID_QUOTE"),
    }
    assert snapshot_uid


def test_v5_manifest_seals_the_exact_primary_minute_cohort(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """A v5 replay must retain the expected legal minute and its real denominator."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, bar_uids, instruments, _, _ = _metric_inputs(m3_runtime)
    with runtime.read_connection() as connection:
        target = str(
            connection.exec_driver_sql(
                "SELECT source_time FROM tdx_bar WHERE bar_uid=?", (bar_uids[0],)
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)

    manifest_uid = snapshots.create_manifest(
        [quote_uid],
        AS_OF,
        bar_uids=bar_uids,
        primary_quote_statuses=(
            PrimaryQuoteStatus(instruments[0], quote_uid, "VALID"),
            PrimaryQuoteStatus(instruments[1], None, "MISSING"),
            PrimaryQuoteStatus(instruments[2], None, "SUSPENDED"),
            PrimaryQuoteStatus(instruments[3], None, "NO_VALID_QUOTE"),
        ),
        historical_windows={"turnover_same_clock": ["2026-08-21"]},
        realtime_quote_uids=[quote_uid],
        realtime_current_bar_uids=[bar_uids[0]],
        realtime_minute_cohort={
            "targets": [{"market": 1, "source_time": target}],
            "expected_primary_count": 4,
            "known_suspended_count": 1,
            "valid_latest_complete_count": 1,
            "missing_or_invalid_count": 2,
            "coverage_ppm": 333_333,
            "source_time_integrity": "EXACT_TARGET",
            "request_count": 3,
            "worker_count": 4,
            "duration_ms": 1,
            "failure_counts": {"NON_TARGET": 2},
        },
    )

    document = snapshots.replay_manifest(manifest_uid)
    assert document["schema_version"] == 5
    assert document["realtime_minute_cohort"]["targets"] == [{"market": 1, "source_time": target}]
    assert document["realtime_minute_cohort"]["coverage_ppm"] == 333_333


def test_v5_snapshot_rejects_a_current_bar_outside_its_legal_minute_cohort(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """A valid-looking bar from another minute cannot seal a fresh cohort."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, bar_uids, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    with runtime.read_connection() as connection:
        target = str(
            connection.exec_driver_sql(
                "SELECT source_time FROM tdx_bar WHERE bar_uid=?", (bar_uids[0],)
            ).scalar_one()
        )
    stale_target = (
        (datetime.fromisoformat(target.replace("Z", "+00:00")) - timedelta(minutes=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    manifest_uid = snapshots.create_manifest(
        [quote_uid],
        AS_OF,
        bar_uids=bar_uids,
        primary_quote_statuses=(
            PrimaryQuoteStatus(instruments[0], quote_uid, "VALID"),
            PrimaryQuoteStatus(instruments[1], None, "MISSING"),
            PrimaryQuoteStatus(instruments[2], None, "SUSPENDED"),
            PrimaryQuoteStatus(instruments[3], None, "NO_VALID_QUOTE"),
        ),
        historical_windows={"turnover_same_clock": ["2026-08-21"]},
        realtime_quote_uids=[quote_uid],
        realtime_current_bar_uids=[bar_uids[0]],
        realtime_minute_cohort={
            "targets": [{"market": 1, "source_time": stale_target}],
            "expected_primary_count": 4,
            "known_suspended_count": 1,
            "valid_latest_complete_count": 1,
            "missing_or_invalid_count": 2,
            "coverage_ppm": 333_333,
            "source_time_integrity": "EXACT_TARGET",
            "request_count": 3,
            "worker_count": 4,
            "duration_ms": 1,
            "failure_counts": {"NON_TARGET": 2},
        },
    )
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instruments[0], identity_uid)])
    with pytest.raises(SnapshotFreshnessError, match="legal minute cohort"):
        snapshots.create_snapshot(
            subject_uid,
            manifest_uid,
            bundle_uid,
            "SHADOW",
            ["QUOTES"],
            [],
            max_skew_ms=61_000,
            max_age_ms=61_000,
        )


def test_v5_snapshot_rejects_a_bar_matched_to_the_other_market_target(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """A Shenzhen bar may not borrow the Shanghai legal-minute target (or vice versa)."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, bar_uids, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    references = ReferenceRepository(runtime, writer)
    shenzhen_uid = references.create_instrument("STOCK", AS_OF - timedelta(days=1))
    references.add_instrument_identity_version(
        shenzhen_uid,
        "SZSE",
        "000001",
        "Shenzhen fixture",
        "LISTED",
        "TRADING",
        AS_OF - timedelta(days=1),
    )
    references.map_instrument("NATIVE_TDX", "0:000001", shenzhen_uid, AS_OF - timedelta(days=1))
    with runtime.read_connection() as connection:
        epoch_uid = str(
            connection.exec_driver_sql(
                "SELECT epoch_uid FROM tdx_bar WHERE bar_uid=?", (bar_uids[0],)
            ).scalar_one()
        )
        shanghai_target = str(
            connection.exec_driver_sql(
                "SELECT source_time FROM tdx_bar WHERE bar_uid=?", (bar_uids[0],)
            ).scalar_one()
        )
    shenzhen_target = datetime.fromisoformat(shanghai_target.replace("Z", "+00:00")) - timedelta(
        minutes=1
    )
    TdxStorage(runtime, writer).record_bars(
        epoch_uid,
        (
            TdxBar(
                TdxInstrument(0, "000001", "STOCK"),
                "1m",
                shenzhen_target,
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
    with runtime.read_connection() as connection:
        shenzhen_bar_uid = str(
            connection.exec_driver_sql(
                "SELECT bar_uid FROM tdx_bar WHERE epoch_uid=? AND instrument_uid=?",
                (epoch_uid, shenzhen_uid),
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    manifest_uid = snapshots.create_manifest(
        [quote_uid],
        AS_OF,
        bar_uids=[*bar_uids, shenzhen_bar_uid],
        primary_quote_statuses=(
            PrimaryQuoteStatus(instruments[0], quote_uid, "VALID"),
            PrimaryQuoteStatus(instruments[1], None, "MISSING"),
            PrimaryQuoteStatus(instruments[2], None, "SUSPENDED"),
            PrimaryQuoteStatus(instruments[3], None, "NO_VALID_QUOTE"),
            PrimaryQuoteStatus(shenzhen_uid, None, "MISSING"),
        ),
        historical_windows={"turnover_same_clock": ["2026-08-21"]},
        realtime_quote_uids=[quote_uid],
        realtime_current_bar_uids=[bar_uids[0], shenzhen_bar_uid],
        realtime_minute_cohort={
            "targets": [
                {"market": 0, "source_time": shanghai_target},
                {"market": 1, "source_time": format_rfc3339(shenzhen_target)},
            ],
            "expected_primary_count": 5,
            "known_suspended_count": 1,
            "valid_latest_complete_count": 2,
            "missing_or_invalid_count": 2,
            "coverage_ppm": 500_000,
            "source_time_integrity": "EXACT_TARGET",
            "request_count": 4,
            "worker_count": 4,
            "duration_ms": 1,
            "failure_counts": {},
        },
    )
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instruments[0], identity_uid)])

    with pytest.raises(SnapshotFreshnessError, match="market target"):
        snapshots.create_snapshot(
            subject_uid,
            manifest_uid,
            bundle_uid,
            "SHADOW",
            ["QUOTES"],
            [],
            max_skew_ms=61_000,
            max_age_ms=61_000,
        )


def test_v1_manifest_remains_unchanged_and_replayable(m3_runtime: tuple[Any, Any, Any]) -> None:
    """Fail if version-2 support rewrites an existing version-1 manifest."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, _, _, _, _ = _metric_inputs(m3_runtime)
    snapshots = SnapshotBuilder(runtime, writer, artifacts)

    manifest_uid = snapshots.create_manifest([quote_uid], AS_OF)

    assert snapshots.replay_manifest(manifest_uid) == {
        "as_of_time": "2026-08-24T01:35:00.000000Z",
        "quote_uids": [quote_uid],
    }
    assert snapshots.manifest_schema_version(snapshots.replay_manifest(manifest_uid)) == 1


def test_v2_replay_rejects_manifest_without_primary_status_inventory(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if a stored version-2 replay can omit its 5,216-style status inventory."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, bar_uids, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    manifest_uid = _store_manifest_document(
        runtime,
        writer,
        artifacts,
        {
            "schema_version": 2,
            "as_of_time": "2026-08-24T01:35:00.000000Z",
            "quote_uids": [quote_uid],
            "bar_uids": bar_uids,
            "historical_windows": {},
        },
    )
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instruments[0], identity_uid)])

    with pytest.raises(SnapshotError, match="primary_quote_statuses"):
        snapshots.create_snapshot(
            subject_uid, manifest_uid, bundle_uid, "SHADOW", ["QUOTES"], [], max_skew_ms=1_000
        )


def test_v2_replay_rejects_quote_claimed_by_the_wrong_primary_instrument(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if a stored manifest rewrites a quote's canonical instrument."""
    runtime, writer, artifacts = m3_runtime
    quote_uid, bar_uids, instruments, subject_uid, identity_uid = _metric_inputs(m3_runtime)
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    manifest_uid = _store_manifest_document(
        runtime,
        writer,
        artifacts,
        {
            "schema_version": 2,
            "as_of_time": "2026-08-24T01:35:00.000000Z",
            "quote_uids": [quote_uid],
            "bar_uids": bar_uids,
            "primary_quote_statuses": [
                {
                    "instrument_uid": instruments[1],
                    "quote_uid": quote_uid,
                    "status": "VALID",
                }
            ],
            "historical_windows": {},
        },
    )
    bundle_uid = snapshots.create_reference_bundle([("INSTRUMENT", instruments[0], identity_uid)])

    with pytest.raises(SnapshotError, match="quote instrument"):
        snapshots.create_snapshot(
            subject_uid,
            manifest_uid,
            bundle_uid,
            "SHADOW",
            ["QUOTES"],
            [],
            max_skew_ms=1_000,
        )


def _store_manifest_document(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    document: dict[str, Any],
) -> str:
    digest = canonical_hash(document)
    artifact = artifacts.put_bytes(
        canonical_bytes(document), "application/vnd.market-monitor.input-manifest+json"
    )
    artifacts.register(artifact)
    manifest_uid = new_uid()

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO input_manifest"
            "(manifest_uid,canonical_hash,artifact_sha256,as_of_time,created_at) "
            "VALUES (?,?,?,?,?)",
            (manifest_uid, digest, artifact.sha256, document["as_of_time"], document["as_of_time"]),
        )

    writer.submit(command).result()
    return manifest_uid
