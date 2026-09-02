from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_analysis.facts import (
    FactExecutor,
    SnapshotNotSealedError,
    UnknownFactCodeError,
)
from market_monitor_analysis.guardian import GuardianService
from market_monitor_analysis.guardian_view import map_guardian_view
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_analysis.state import StateService
from market_monitor_analysis.thresholds import ThresholdRegistry
from market_monitor_data.health import (
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
)
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue
from sqlalchemy.exc import NoResultFound


def _state_with_metrics(
    runtime: Any,
    writer: Any,
    artifacts: Any,
    metrics: dict[str, int],
    *,
    availability: str = "AVAILABLE",
    disposition: str = "SHADOW",
    fill_defaults: bool = True,
    scout_metrics: dict[str, int] | None = None,
) -> tuple[str, str]:
    complete_metrics = {
        "RISE_RATE_PPM": 0,
        "HEAD_CONCENTRATION_PPM": 0,
        "INTERNAL_DIVERGENCE_PPM": 0,
        "CROWDING_PPM": 0,
        "LIQUIDITY_WEAKENING_PPM": 0,
        "CORE_WEAKENING_PPM": 0,
        "BREADTH_COLLAPSE_PPM": 0,
        "STAMPEDE_RISK_PPM": 0,
        "T1_CHASING_RISK_PPM": 0,
        "EARLY_SIGNAL_FAILURE_PPM": 0,
    }
    complete_metrics = complete_metrics if fill_defaults else {}
    complete_metrics.update(metrics)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    instrument = references.create_instrument("STOCK", now)
    provider = f"FIXTURE-{instrument}"
    trading_code = instrument[-8:]
    identity = references.add_instrument_identity_version(
        instrument, "SSE", trading_code, "Fixture", "LISTED", "TRADING", now
    )
    references.map_instrument(provider, trading_code, instrument, now)
    subject = references.ensure_analysis_subject("INSTRUMENT", instrument)
    epoch = SourceEpochService(runtime, writer).start_epoch(provider, "guardian-v1", now)
    CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 1000, 60)).record(
        epoch, "QUOTES", 1_000_000, 1, now
    )
    ingestion = IngestionService(runtime, writer, artifacts).ingest(
        epoch,
        ProviderBatch(
            "guardian-batch",
            "2026-08-04T00:00:01Z",
            (ProviderRecord(trading_code, "2026-08-04T00:00:00Z", "10.0000", 1, {}),),
        ),
    )
    with runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?", (ingestion.lineages[0],)
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    bundle = snapshots.create_reference_bundle([("INSTRUMENT", instrument, identity)])
    manifest = snapshots.create_manifest([quote_uid], datetime(2026, 8, 4, 0, 0, 30, tzinfo=UTC))
    snapshot = snapshots.create_snapshot(
        subject, manifest, bundle, disposition, ["QUOTES"], [], max_skew_ms=1000
    )
    snapshots.seal(snapshot)
    facts = FactExecutor(runtime, writer)
    thresholds = ThresholdRegistry(runtime, writer)
    guardian_threshold_uid = thresholds.resolve_explicit(
        "GUARDIAN", "guardian-thresholds-v1.0-prod", now
    ).uid
    scout_threshold_uid = thresholds.resolve_explicit(
        "SCOUT", "scout-thresholds-v1.0-prod", now
    ).uid
    objective = facts.execute(snapshot, "OBJECTIVE_QUOTE_SUMMARY", "1")
    guardian_facts = facts.record_metrics(
        snapshot,
        "GUARDIAN_INPUTS",
        "m4-fixture-metrics-v1",
        complete_metrics,
        threshold_version_uid=guardian_threshold_uid,
    )
    scout_facts = (
        ()
        if scout_metrics is None
        else facts.record_scout_metrics(
            snapshot,
            "SCOUT_INPUTS",
            "m4-fixture-metrics-v1",
            scout_metrics,
            threshold_version_uid=scout_threshold_uid,
        )
    )
    state = StateService(runtime, writer).evaluate(
        snapshot,
        availability,
        "OBSERVING" if availability == "AVAILABLE" else None,
        [fact.fact_uid for fact in (*objective, *guardian_facts, *scout_facts)],
        0,
    )
    return state.evaluation_uid, subject


@pytest.mark.parametrize(
    ("code", "risk"),
    [
        ("RISE_RATE_PPM", "RISING_TOO_FAST"),
        ("HEAD_CONCENTRATION_PPM", "HEAD_CONCENTRATION_HIGH"),
        ("INTERNAL_DIVERGENCE_PPM", "INTERNAL_DIVERGENCE"),
        ("CROWDING_PPM", "CROWDING_INCREASING"),
        ("LIQUIDITY_WEAKENING_PPM", "LIQUIDITY_WEAKENING"),
        ("CORE_WEAKENING_PPM", "CORE_MEMBERS_WEAKENING"),
        ("BREADTH_COLLAPSE_PPM", "BREADTH_COLLAPSING"),
        ("STAMPEDE_RISK_PPM", "STAMPEDE_RISK"),
        ("T1_CHASING_RISK_PPM", "T1_CHASING_RISK"),
        ("EARLY_SIGNAL_FAILURE_PPM", "EARLY_SIGNAL_FAILED"),
    ],
)
def test_each_guardian_rule_is_fact_backed_and_deterministic(
    m4_runtime: tuple[Any, Any, Any], code: str, risk: str
) -> None:
    runtime, writer, artifacts = m4_runtime
    state_uid, _ = _state_with_metrics(runtime, writer, artifacts, {code: 1_000_000})
    guardian = GuardianService(runtime, writer)
    first = guardian.evaluate(state_uid)
    second = guardian.evaluate(state_uid)
    assert first == second
    finding = next(item for item in first.risks if item.risk_tag == risk)
    assert finding.primary_fact_uid is not None


def test_precedence_t1_data_pause_view_and_lifecycle_separation(
    m4_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m4_runtime
    state_uid, _ = _state_with_metrics(
        runtime,
        writer,
        artifacts,
        {"T1_CHASING_RISK_PPM": 1_000_000, "LIQUIDITY_WEAKENING_PPM": 1_000_000},
    )
    result = GuardianService(runtime, writer).evaluate(state_uid)
    assert result.guardian_effect == "SUPPRESS" and result.blocking
    with runtime.read_connection() as connection:
        lifecycle = connection.exec_driver_sql(
            "SELECT lifecycle_state FROM state_evaluation WHERE evaluation_uid=?", (state_uid,)
        ).scalar_one()
    assert lifecycle == "OBSERVING"
    view = map_guardian_view(result)
    assert view.status == "BLOCKED"
    assert all("safe" not in reason.reason_code.lower() for reason in view.reasons)

    warming_uid, _ = _state_with_metrics(runtime, writer, artifacts, {}, availability="WARMING_UP")
    paused = GuardianService(runtime, writer).evaluate(warming_uid)
    assert paused.guardian_effect == "PAUSE" and paused.blocking
    assert {risk.risk_tag for risk in paused.risks} == {"DATA_LIMITATION"}


def test_undeclared_or_advice_like_guardian_fact_codes_are_rejected(
    m4_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m4_runtime
    state_uid, _ = _state_with_metrics(runtime, writer, artifacts, {})
    with runtime.read_connection() as connection:
        snapshot_uid = str(
            connection.exec_driver_sql(
                "SELECT snapshot_uid FROM state_evaluation WHERE evaluation_uid=?", (state_uid,)
            ).scalar_one()
        )
    with pytest.raises(UnknownFactCodeError):
        FactExecutor(runtime, writer).record_metrics(
            snapshot_uid, "BAD", "1", {"BUY_SIGNAL": 1}, threshold_version_uid="unused"
        )
    with runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT subject_uid,manifest_uid,bundle_uid FROM evaluation_snapshot "
            "WHERE snapshot_uid=?",
            (snapshot_uid,),
        ).one()
    draft = SnapshotBuilder(runtime, writer, artifacts).create_snapshot(
        str(row.subject_uid),
        str(row.manifest_uid),
        str(row.bundle_uid),
        "HISTORICAL_REPLAY",
        ["QUOTES"],
        [],
        max_skew_ms=1000,
    )
    with pytest.raises(SnapshotNotSealedError):
        FactExecutor(runtime, writer).record_metrics(
            draft,
            "GUARDIAN_INPUTS",
            "1",
            {"T1_CHASING_RISK_PPM": 1},
            threshold_version_uid=ThresholdRegistry(runtime, writer)
            .resolve_explicit(
                "GUARDIAN",
                "guardian-thresholds-v1.0-prod",
                datetime(2026, 8, 4, tzinfo=UTC),
            )
            .uid,
        )


def test_missing_facts_pause_nonofficial_isolated_and_missing_state_rolls_back(
    m4_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m4_runtime
    missing_facts_uid, _ = _state_with_metrics(runtime, writer, artifacts, {}, fill_defaults=False)
    limited = GuardianService(runtime, writer).evaluate(missing_facts_uid)
    assert limited.guardian_effect == "PAUSE"
    assert limited.risks[0].reason_code == "REQUIRED_GUARDIAN_FACTS_MISSING"

    user_uid, _ = _state_with_metrics(runtime, writer, artifacts, {}, disposition="USER_QUERY")
    user_guardian = GuardianService(runtime, writer).evaluate(user_uid)
    assert user_guardian.guardian_effect == "ALLOW"
    with runtime.read_connection() as connection:
        projections_before = int(
            connection.exec_driver_sql("SELECT count(*) FROM current_state_projection").scalar_one()
        )
        guardians_before = int(
            connection.exec_driver_sql("SELECT count(*) FROM guardian_evaluation").scalar_one()
        )
    assert projections_before == 0
    with pytest.raises(NoResultFound):
        GuardianService(runtime, writer).evaluate("missing-state")
    with runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM guardian_evaluation").scalar_one()
            == guardians_before
        )


def test_guardian_result_and_view_survive_restart(
    m4_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m4_runtime
    state_uid, _ = _state_with_metrics(
        runtime, writer, artifacts, {"BREADTH_COLLAPSE_PPM": 1_000_000}
    )
    expected = GuardianService(runtime, writer).evaluate(state_uid)
    expected_view = map_guardian_view(expected)
    paths = runtime.paths
    writer.close()
    runtime.close()

    reopened = DatabaseRuntime.open(paths)
    MigrationManager().upgrade(reopened)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        actual = GuardianService(reopened, reopened_writer).evaluate(state_uid)
        assert actual == expected
        assert map_guardian_view(actual) == expected_view
    finally:
        reopened_writer.close()
        reopened.close()
