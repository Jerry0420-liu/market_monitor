"""CR-004 runtime context wiring."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from market_monitor_analysis.metric_runner import MetricRunner
from market_monitor_analysis.snapshots import PrimaryQuoteStatus, SnapshotBuilder
from market_monitor_data.health import CapabilityHealthService, HealthThresholds
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import TdxInstrument, TdxQuoteDetail
from market_monitor_data.tdx.storage import TdxStorage
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

from tests.cr004.conftest import AS_OF, _bar


def test_runner_uses_bundle_pinned_context_mapping_and_canonical_context_data(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if ETF/style inputs stay synthetic or unversioned at runtime."""
    runtime, writer, artifacts = m3_runtime
    original_snapshot_uid = sealed_metric_snapshot("SHADOW")
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    with runtime.read_connection() as connection:
        original = connection.exec_driver_sql(
            "SELECT s.subject_uid,s.bundle_uid,s.manifest_uid,a.sector_uid,c.epoch_uid "
            "FROM evaluation_snapshot s JOIN analysis_subject a ON a.subject_uid=s.subject_uid "
            "JOIN capability_snapshot c ON c.snapshot_uid=s.snapshot_uid "
            "WHERE s.snapshot_uid=? ORDER BY c.capability LIMIT 1",
            (original_snapshot_uid,),
        ).one()
        entries = [
            (str(row.entity_kind), str(row.entity_uid), str(row.version_uid))
            for row in connection.exec_driver_sql(
                "SELECT entity_kind,entity_uid,version_uid FROM reference_version_entry "
                "WHERE bundle_uid=? ORDER BY entity_kind,entity_uid",
                (original.bundle_uid,),
            ).all()
        ]
    manifest = snapshots.replay_manifest(str(original.manifest_uid))
    references = ReferenceRepository(runtime, writer)
    valid_from = AS_OF - timedelta(days=60)
    contexts = (
        ("510300", "ETF"),
        ("000300", "INDEX"),
    )
    context_uids: dict[str, str] = {}
    identity_uids: dict[str, str] = {}
    for code, kind in contexts:
        instrument_uid = references.create_instrument(kind, valid_from)
        identity_uids[code] = references.add_instrument_identity_version(
            instrument_uid,
            "SSE",
            code,
            code,
            "LISTED",
            "TRADING",
            valid_from,
        )
        references.map_instrument("NATIVE_TDX", f"1:{code}", instrument_uid, valid_from)
        context_uids[code] = instrument_uid
    source = artifacts.put_bytes(b"owner-approved-context-mapping", "application/json")
    artifacts.register(source)
    mapping = references.add_context_mapping_version(
        str(original.sector_uid),
        etf_instrument_uid=context_uids["510300"],
        style_instrument_uid=context_uids["000300"],
        owner_approval_ref="CR-004 test approval",
        source_artifact_sha256=source.sha256,
        valid_from=valid_from,
    )
    IngestionService(runtime, writer, artifacts).ingest(
        str(original.epoch_uid),
        ProviderBatch(
            "cr004-context-fixture",
            "2026-08-24T01:44:30.000000Z",
            tuple(
                ProviderRecord(
                    f"1:{code}",
                    "2026-08-24T01:44:00.000000Z",
                    "11",
                    100,
                    {},
                )
                for code, _ in contexts
            ),
        ),
    )
    with runtime.read_connection() as connection:
        quote_rows = connection.exec_driver_sql(
            "SELECT q.quote_uid,l.instrument_uid FROM market_quote q "
            "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
            "WHERE l.epoch_uid=? AND l.instrument_uid IN (?,?) ORDER BY l.instrument_uid",
            (
                str(original.epoch_uid),
                context_uids["510300"],
                context_uids["000300"],
            ),
        ).all()
    TdxStorage(runtime, writer).record_quote_details(
        [
            (
                str(row.quote_uid),
                TdxQuoteDetail(
                    pre_close=Decimal("10"),
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    amount=Decimal("1000"),
                    quote_status="VALUE",
                    server_time_raw=94400,
                ),
            )
            for row in quote_rows
        ]
    )
    bars = tuple(
        _bar(
            TdxInstrument(1, code, kind),
            AS_OF - timedelta(minutes=offset),
            "1m",
            Decimal("100"),
        )
        for code, kind in contexts
        for offset in range(15, 0, -1)
    )
    TdxStorage(runtime, writer).record_bars(str(original.epoch_uid), bars)
    with runtime.read_connection() as connection:
        context_bar_uids = [
            str(row.bar_uid)
            for row in connection.exec_driver_sql(
                "SELECT bar_uid FROM tdx_bar WHERE epoch_uid=? "
                "AND instrument_uid IN (?,?) ORDER BY bar_uid",
                (
                    str(original.epoch_uid),
                    context_uids["510300"],
                    context_uids["000300"],
                ),
            ).all()
        ]
    context_quote_uids = [str(row.quote_uid) for row in quote_rows]
    primary_statuses = tuple(
        PrimaryQuoteStatus(
            str(item["instrument_uid"]),
            None if item["quote_uid"] is None else str(item["quote_uid"]),
            cast(
                Literal["VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"],
                str(item["status"]),
            ),
        )
        for item in manifest["primary_quote_statuses"]
    )
    manifest_uid = snapshots.create_manifest(
        manifest["quote_uids"],
        AS_OF,
        bar_uids=[*manifest["bar_uids"], *context_bar_uids],
        context_quote_uids=context_quote_uids,
        primary_quote_statuses=primary_statuses,
        historical_windows=manifest["historical_windows"],
    )
    bundle_uid = snapshots.create_reference_bundle(
        [
            *entries,
            ("INSTRUMENT", context_uids["510300"], identity_uids["510300"]),
            ("INSTRUMENT", context_uids["000300"], identity_uids["000300"]),
            ("MAPPING", str(original.sector_uid), mapping.mapping_version_uid),
        ]
    )
    snapshot_uid = snapshots.create_snapshot(
        str(original.subject_uid),
        manifest_uid,
        bundle_uid,
        "SHADOW",
        ["QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"],
        [],
        max_skew_ms=2_000_000_000,
    )
    snapshots.seal(snapshot_uid)

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    with artifacts.open_verified(result.evidence_sha256) as stream:
        evidence = json.load(stream)
    metrics = {item["code"]: item for item in evidence["metric_output"]["metrics"]}
    assert metrics["ETF_CONFIRMATION_PPM"]["status"] == "VALUE"
    assert metrics["STYLE_SUPPORT_PPM"]["status"] == "VALUE"
    assert (
        evidence["input_lineage"]["context_mapping"]["version_uid"] == mapping.mapping_version_uid
    )


def test_runner_reports_absent_owner_context_mapping_as_not_applicable(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if an unconfigured context is presented as live-validated."""
    runtime, writer, artifacts = m3_runtime
    result = MetricRunner(runtime, writer, artifacts).run(
        sealed_metric_snapshot("SHADOW"),
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    with artifacts.open_verified(result.evidence_sha256) as stream:
        evidence = json.load(stream)
    metrics = {item["code"]: item for item in evidence["metric_output"]["metrics"]}
    assert metrics["ETF_CONFIRMATION_PPM"]["status"] == "NOT_APPLICABLE"
    assert metrics["STYLE_SUPPORT_PPM"]["status"] == "NOT_APPLICABLE"
    assert evidence["input_lineage"]["context_mapping"] == {
        "status": "NOT_APPLICABLE",
        "reason": "NO_OWNER_APPROVED_MAPPING",
    }


def test_runner_marks_the_exact_final_continuous_close_snapshot_as_final(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if a valid 15:00 snapshot is rejected only because the phase is CLOSED."""
    runtime, writer, artifacts = m3_runtime
    original_snapshot_uid = sealed_metric_snapshot("SHADOW")
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    with runtime.read_connection() as connection:
        original = connection.exec_driver_sql(
            "SELECT subject_uid,bundle_uid,manifest_uid FROM evaluation_snapshot "
            "WHERE snapshot_uid=?",
            (original_snapshot_uid,),
        ).one()
        epoch_uid = connection.exec_driver_sql(
            "SELECT epoch_uid FROM capability_snapshot WHERE snapshot_uid=? "
            "ORDER BY capability LIMIT 1",
            (original_snapshot_uid,),
        ).scalar_one()
    final_close = datetime(2026, 8, 24, 7, tzinfo=UTC)
    health = CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 60, 120))
    for capability in ("QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"):
        health.record(str(epoch_uid), capability, 1_000_000, 1, final_close)
    manifest = snapshots.replay_manifest(str(original.manifest_uid))
    primary_statuses = tuple(
        PrimaryQuoteStatus(
            str(item["instrument_uid"]),
            None if item["quote_uid"] is None else str(item["quote_uid"]),
            cast(
                Literal["VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"],
                str(item["status"]),
            ),
        )
        for item in manifest["primary_quote_statuses"]
    )
    manifest_uid = snapshots.create_manifest(
        manifest["quote_uids"],
        final_close,
        bar_uids=manifest["bar_uids"],
        primary_quote_statuses=primary_statuses,
        historical_windows=manifest["historical_windows"],
    )
    snapshot_uid = snapshots.create_snapshot(
        str(original.subject_uid),
        manifest_uid,
        str(original.bundle_uid),
        "SHADOW",
        ["QUOTES", "MINUTE_BARS", "DAILY_BARS", "TDX_BLOCKS"],
        [],
        max_skew_ms=2_000_000_000,
    )
    snapshots.seal(snapshot_uid)

    result = MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    with artifacts.open_verified(result.evidence_sha256) as stream:
        evidence = json.load(stream)
    assert evidence["metric_output"]["is_final_trading_snapshot"] is True
    assert evidence["metric_output"]["quality"]["fitness_status"] == "FIT"
