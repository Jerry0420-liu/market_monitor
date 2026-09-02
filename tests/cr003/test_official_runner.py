from __future__ import annotations

from pathlib import Path

import pytest
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager

from scripts.official_runner import OfficialRuntimeSettings, run_official_once


class _NoNetworkGateway:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"disabled official runtime touched TDX: {name}")


def test_staged_disabled_runner_migrates_without_business_side_effects(tmp_path: Path) -> None:
    settings = OfficialRuntimeSettings(tmp_path)

    report = run_official_once(settings, gateway=_NoNetworkGateway())  # type: ignore[arg-type]

    assert report == {
        "ok": True,
        "ready_for_official": False,
        "official_gate": "CLOSED",
        "threshold_activation": 0,
        "side_effects": "DISABLED",
    }
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    try:
        assert MigrationManager().verify(runtime) == "0014_cr003_official_cycle_journal"
        with runtime.read_connection() as connection:
            for table in (
                "analysis_commit",
                "market_event",
                "notification_intent",
                "delivery_attempt",
                "official_cycle_run",
                "threshold_activation",
            ):
                assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
    finally:
        runtime.close()


def test_zero_threshold_activation_stays_closed_without_touching_tdx(tmp_path: Path) -> None:
    settings = OfficialRuntimeSettings(
        tmp_path,
        official_enabled=True,
        threshold_activation=0,
        subject_uid="subject-that-must-not-be-used",
    )

    report = run_official_once(settings, gateway=_NoNetworkGateway())  # type: ignore[arg-type]

    assert report["official_gate"] == "CLOSED"
    assert report["threshold_activation"] == 0
    assert report["reason"] == "THRESHOLD_ACTIVATION_UNAVAILABLE"


def test_disabled_runtime_rejects_nonzero_threshold_activation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="threshold_activation=0"):
        OfficialRuntimeSettings(tmp_path, threshold_activation=1)
