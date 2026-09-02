from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_analysis.facts import FactExecutor, SnapshotNotSealedError
from market_monitor_analysis.snapshots import (
    FutureInputError,
    RequiredCapabilityError,
    SnapshotBuilder,
)
from market_monitor_analysis.state import (
    OfficialStateMutationError,
    StateService,
)
from market_monitor_data.health import (
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
    WatermarkRepository,
)
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository


def _seed(m3_runtime: tuple[Any, Any, Any]) -> tuple[Any, ...]:
    runtime, writer, artifacts = m3_runtime
    now = datetime(2026, 8, 4, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    instrument = references.create_instrument("STOCK", now)
    identity = references.add_instrument_identity_version(
        instrument, "SSE", "600000", "Fixture", "LISTED", "TRADING", now
    )
    references.map_instrument("FIXTURE", "600000", instrument, now)
    subject = references.ensure_analysis_subject("INSTRUMENT", instrument)
    epoch = SourceEpochService(runtime, writer).start_epoch("FIXTURE", "v1", now)
    CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 2_000, 120)).record(
        epoch, "QUOTES", 1_000_000, 10, now
    )
    result = IngestionService(runtime, writer, artifacts).ingest(
        epoch,
        ProviderBatch(
            "snapshot-batch",
            "2026-08-04T00:00:01Z",
            (ProviderRecord("600000", "2026-08-04T00:00:00Z", "10.0000", 100, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?",
                (result.lineages[0],),
            ).scalar_one()
        )
    return runtime, writer, artifacts, subject, instrument, epoch, identity, quote_uid


def test_public_state_service_rejects_official_projection_mutation(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts, subject, instrument, _, identity, quote_uid = _seed(m3_runtime)
    as_of = datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC)
    builder = SnapshotBuilder(runtime, writer, artifacts)
    bundle = builder.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    manifest = builder.create_manifest([quote_uid], as_of)
    snapshot = builder.create_snapshot(
        subject,
        manifest,
        bundle,
        "OFFICIAL",
        ["QUOTES"],
        [],
        max_skew_ms=1_000,
    )
    builder.seal(snapshot)
    facts = FactExecutor(runtime, writer).execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")

    with pytest.raises(OfficialStateMutationError, match="AnalysisCommit"):
        StateService(runtime, writer).evaluate(
            snapshot,
            "AVAILABLE",
            "OBSERVING",
            [fact.fact_uid for fact in facts],
            0,
        )

    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM state_evaluation").scalar_one() == 0
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM current_state_projection").scalar_one()
            == 0
        )


def test_manifest_snapshot_fact_replay_and_nonofficial_state_isolation(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts, subject, instrument, epoch, identity, quote_uid = _seed(m3_runtime)
    as_of = datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC)
    builder = SnapshotBuilder(runtime, writer, artifacts)
    bundle = builder.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    assert bundle == builder.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    manifest = builder.create_manifest([quote_uid, quote_uid], as_of)
    assert manifest == builder.create_manifest([quote_uid], as_of)
    snapshot = builder.create_snapshot(
        subject, manifest, bundle, "SHADOW", ["QUOTES"], [], max_skew_ms=1_000
    )
    facts = FactExecutor(runtime, writer)
    with pytest.raises(SnapshotNotSealedError):
        facts.execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")
    builder.seal(snapshot)
    first = facts.execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")
    second = facts.execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")
    assert [fact.fact_hash for fact in first] == [fact.fact_hash for fact in second]
    assert builder.replay_manifest(manifest)["quote_uids"] == [quote_uid]
    baseline = facts.record_baseline(subject, first[0].fact_uid, "DAILY", "2026-08-04")
    assert baseline.version == 1

    states = StateService(runtime, writer)
    shadow = states.evaluate(
        snapshot, "AVAILABLE", "OBSERVING", [fact.fact_uid for fact in first], 0
    )
    assert shadow.projection is None
    user_snapshot = builder.create_snapshot(
        subject, manifest, bundle, "USER_QUERY", ["QUOTES"], [], max_skew_ms=1_000
    )
    builder.seal(user_snapshot)
    user_facts = facts.execute(user_snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")
    watermark_before = WatermarkRepository(runtime, writer)
    watermark_before.advance(epoch, "QUOTES", as_of, as_of + timedelta(seconds=1))
    user = states.evaluate(
        user_snapshot, "AVAILABLE", "STARTING", [fact.fact_uid for fact in user_facts], 0
    )
    assert user.projection is None
    assert WatermarkRepository(runtime, writer).get(epoch, "QUOTES").version == 1

    with runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM current_state_projection").scalar_one()
            == 0
        )


def test_future_inputs_and_expired_required_health_are_rejected(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts, subject, instrument, _, identity, quote_uid = _seed(m3_runtime)
    builder = SnapshotBuilder(runtime, writer, artifacts)
    with pytest.raises(FutureInputError):
        builder.create_manifest([quote_uid], datetime(2026, 8, 4, tzinfo=UTC))
    as_of = datetime(2026, 8, 4, 0, 3, tzinfo=UTC)
    manifest = builder.create_manifest([quote_uid], as_of)
    bundle = builder.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    with pytest.raises(RequiredCapabilityError):
        builder.create_snapshot(
            subject, manifest, bundle, "OFFICIAL", ["QUOTES"], [], max_skew_ms=1_000
        )
