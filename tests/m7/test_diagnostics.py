import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from market_monitor_api.security import OwnerSecurity
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue

from tests.m7.test_security import MutableClock


def test_m7_diagnostics_reports_safe_empty_runtime(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    runtime.close()
    result = subprocess.run(
        [sys.executable, "scripts/m7_diagnostics.py", "--data-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "active_sessions": 0,
        "analysis_queries": {},
        "migration_revision": "0014_cr003_official_cycle_journal",
        "notification_setting": {
            "enabled": False,
            "endpoint_source": "ENVIRONMENT",
            "version": 1,
        },
        "owner_accounts": 0,
        "webhook_endpoint": "ENVIRONMENT_ONLY_NOT_INSPECTED",
    }


def test_m7_diagnostics_does_not_upgrade_an_unready_database(tmp_path: Path) -> None:
    data_directory = tmp_path / "unready"
    result = subprocess.run(
        [sys.executable, "scripts/m7_diagnostics.py", "--data-dir", str(data_directory)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(data_directory))
    try:
        with runtime.read_connection() as connection:
            tables = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).scalars()
            )
        assert "owner_account" not in tables
    finally:
        runtime.close()


def test_m7_diagnostics_excludes_expired_unrevoked_sessions(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        security = OwnerSecurity(
            runtime,
            writer,
            MutableClock(datetime(2020, 1, 1, tzinfo=UTC)),
            session_seconds=60,
        )
        security.bootstrap("expired diagnostic password")
        security.login("owner", "expired diagnostic password", "diagnostic-test")
    finally:
        writer.close()
        runtime.close()

    result = subprocess.run(
        [sys.executable, "scripts/m7_diagnostics.py", "--data-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["active_sessions"] == 0
