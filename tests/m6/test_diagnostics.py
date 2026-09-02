import json
import subprocess
import sys
from pathlib import Path

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager


def test_m6_diagnostics_reports_safe_empty_runtime(tmp_path: Path) -> None:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    runtime.close()
    result = subprocess.run(
        [sys.executable, "scripts/m6_diagnostics.py", "--data-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "attempts": 0,
        "delivery": {},
        "events": {},
        "migration_revision": "0014_cr003_official_cycle_journal",
        "restore_generation": 0,
        "webhook": "DISABLED_BY_DEFAULT",
    }
