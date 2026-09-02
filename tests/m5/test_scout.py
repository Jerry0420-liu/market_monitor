from typing import Any

import pytest
from market_monitor_analysis.facts import FactExecutor, UnknownFactCodeError
from market_monitor_analysis.guardian import GuardianService
from market_monitor_analysis.scout import ScoutService
from market_monitor_analysis.scout_view import map_scout_view
from market_monitor_analysis.state import StateService
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue
from sqlalchemy.exc import NoResultFound

from tests.m4.test_guardian import _state_with_metrics


@pytest.mark.parametrize(
    ("code", "tag"),
    [
        ("EARLY_ACTIVITY_PPM", "EARLY_ACTIVITY"),
        ("HEALTHY_BREADTH_PPM", "HEALTHY_BREADTH"),
        ("RELATIVE_STRENGTH_PPM", "RELATIVE_STRENGTH"),
        ("TURNOVER_CONFIRMATION_PPM", "TURNOVER_CONFIRMATION"),
        ("ETF_CONFIRMATION_PPM", "ETF_CONFIRMATION"),
        ("STYLE_SUPPORT_PPM", "STYLE_SUPPORT"),
        ("LOW_CROWDING_PPM", "LOW_CROWDING"),
        ("CONTINUITY_STRENGTHENING_PPM", "CONTINUITY_STRENGTHENING"),
    ],
)
def test_each_scout_tag_is_fact_backed(
    m5_runtime: tuple[Any, Any, Any], code: str, tag: str
) -> None:
    runtime, writer, artifacts = m5_runtime
    state_uid, _ = _state_with_metrics(
        runtime, writer, artifacts, {}, scout_metrics={code: 1_000_000}
    )
    guardian = GuardianService(runtime, writer).evaluate(state_uid)
    scout = ScoutService(runtime, writer).evaluate(guardian.guardian_uid)
    finding = next(item for item in scout.tags if item.opportunity_tag == tag)
    assert finding.primary_fact_uid
    assert scout == ScoutService(runtime, writer).evaluate(guardian.guardian_uid)


def test_status_strength_guardian_suppression_and_safe_view(
    m5_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m5_runtime
    metrics = {
        "EARLY_ACTIVITY_PPM": 1_000_000,
        "HEALTHY_BREADTH_PPM": 1_000_000,
        "RELATIVE_STRENGTH_PPM": 1_000_000,
        "TURNOVER_CONFIRMATION_PPM": 1_000_000,
        "ETF_CONFIRMATION_PPM": 1_000_000,
        "STYLE_SUPPORT_PPM": 1_000_000,
    }
    state_uid, subject = _state_with_metrics(
        runtime,
        writer,
        artifacts,
        {"T1_CHASING_RISK_PPM": 1_000_000},
        scout_metrics=metrics,
    )
    guardian = GuardianService(runtime, writer).evaluate(state_uid)
    scout = ScoutService(runtime, writer).evaluate(guardian.guardian_uid)
    assert guardian.guardian_effect == "SUPPRESS"
    assert scout.scout_status == "ACTIVE" and scout.scout_strength == "HIGH"
    assert scout.suppressed_by_guardian
    with pytest.raises(NoResultFound):
        StateService(runtime, writer).get_projection(subject)
    view = map_scout_view(scout)
    assert view.suppressed_by_guardian and view.guardian_effect == "SUPPRESS"
    banned = ("buy", "sell", "target", "position", "guarantee", "safe")
    assert all(
        not any(word in reason.reason_code.lower() for word in banned) for reason in view.reasons
    )

    warming_uid, _ = _state_with_metrics(
        runtime,
        writer,
        artifacts,
        {},
        availability="WARMING_UP",
        scout_metrics={"EARLY_ACTIVITY_PPM": 1_000_000},
    )
    paused_guardian = GuardianService(runtime, writer).evaluate(warming_uid)
    paused = ScoutService(runtime, writer).evaluate(paused_guardian.guardian_uid)
    assert paused_guardian.guardian_effect == "PAUSE" and paused.suppressed_by_guardian

    below_uid, _ = _state_with_metrics(
        runtime,
        writer,
        artifacts,
        {},
        scout_metrics={"EARLY_ACTIVITY_PPM": 599_999},
    )
    below_guardian = GuardianService(runtime, writer).evaluate(below_uid)
    below = ScoutService(runtime, writer).evaluate(below_guardian.guardian_uid)
    assert below.scout_status == "NONE" and below.scout_strength == "LOW" and not below.tags


def test_missing_guardian_creates_no_scout_rows(m5_runtime: tuple[Any, Any, Any]) -> None:
    runtime, writer, _ = m5_runtime
    with pytest.raises(NoResultFound):
        ScoutService(runtime, writer).evaluate("missing-guardian")
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM scout_evaluation").scalar_one() == 0


def test_unknown_scout_fact_rejected_and_result_survives_restart(
    m5_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, artifacts = m5_runtime
    state_uid, _ = _state_with_metrics(
        runtime,
        writer,
        artifacts,
        {},
        scout_metrics={"HEALTHY_BREADTH_PPM": 1_000_000},
    )
    with runtime.read_connection() as connection:
        snapshot_uid = str(
            connection.exec_driver_sql(
                "SELECT snapshot_uid FROM state_evaluation WHERE evaluation_uid=?", (state_uid,)
            ).scalar_one()
        )
    with pytest.raises(UnknownFactCodeError):
        FactExecutor(runtime, writer).record_scout_metrics(
            snapshot_uid,
            "BAD_SCOUT",
            "1",
            {"BUY_SIGNAL": 1},
            threshold_version_uid="unused",
        )
    guardian = GuardianService(runtime, writer).evaluate(state_uid)
    expected = ScoutService(runtime, writer).evaluate(guardian.guardian_uid)
    paths = runtime.paths
    writer.close()
    runtime.close()
    reopened = DatabaseRuntime.open(paths)
    MigrationManager().upgrade(reopened)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        actual = ScoutService(reopened, reopened_writer).evaluate(guardian.guardian_uid)
        assert actual == expected
        assert map_scout_view(actual) == map_scout_view(expected)
    finally:
        reopened_writer.close()
        reopened.close()
