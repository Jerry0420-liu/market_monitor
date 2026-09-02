from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "persistence" / "src"))

from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.diagnostics import collect_database_diagnostics  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.writer import WriterQueue  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the M1 SQLite persistence runtime")
    parser.add_argument("--data-dir", required=True, type=Path)
    arguments = parser.parse_args()

    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(arguments.data_dir))
    try:
        MigrationManager(ROOT).verify(runtime)
        artifacts = ArtifactStore(runtime, WriterQueue(runtime))
        result = collect_database_diagnostics(runtime, artifacts)
        print(json.dumps(asdict(result), sort_keys=True))
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
