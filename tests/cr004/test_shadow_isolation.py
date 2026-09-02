"""CR-004 Shadow/Replay runs must not change OFFICIAL projections or delivery state."""

from __future__ import annotations

from collections.abc import Callable

from market_monitor_analysis.metric_runner import MetricRunner
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

_OFFICIAL_TABLES = (
    "current_state_projection",
    "capability_watermark",
    "current_event_projection",
    "notification_delivery_state",
    "market_event",
    "notification_intent",
    "delivery_attempt",
)


def test_shadow_runner_keeps_every_official_projection_and_delivery_table_unchanged(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
    sealed_metric_snapshot: Callable[[str], str],
) -> None:
    """Break if a Shadow metric run writes any OFFICIAL stateful side effect."""
    runtime, writer, artifacts = m3_runtime
    snapshot_uid = sealed_metric_snapshot("SHADOW")
    before = _official_side_effect_counts(runtime)

    MetricRunner(runtime, writer, artifacts).run(
        snapshot_uid,
        guardian_threshold_version="guardian-thresholds-v1.0-prod",
        scout_threshold_version="scout-thresholds-v1.0-prod",
    )

    assert _official_side_effect_counts(runtime) == before


def _official_side_effect_counts(runtime: DatabaseRuntime) -> dict[str, int]:
    with runtime.read_connection() as connection:
        return {
            table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
            for table in _OFFICIAL_TABLES
        }
