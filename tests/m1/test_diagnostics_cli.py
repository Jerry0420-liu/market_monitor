import json
import subprocess
import sys
from pathlib import Path

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager


def test_database_diagnostics_cli_reports_initialized_database(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    runtime.close()

    result = subprocess.run(
        [
            sys.executable,
            "scripts/m1_diagnostics.py",
            "--data-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["journal_mode"] == "wal"
    assert payload["synchronous"] == 2
    assert payload["integrity_check"] == "ok"
    assert payload["migration_revision"] == "0014_cr003_official_cycle_journal"
