from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "persistence" / "src"))

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 Scout diagnostics")
    parser.add_argument("--data-dir", required=True, type=Path)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(parser.parse_args().data_dir))
    try:
        MigrationManager().upgrade(runtime)
        with runtime.read_connection() as connection:
            status = {
                str(row.scout_status): int(row.total)
                for row in connection.exec_driver_sql(
                    "SELECT scout_status,count(*) AS total FROM scout_evaluation "
                    "GROUP BY scout_status ORDER BY scout_status"
                )
            }
            suppressed = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM scout_evaluation WHERE suppressed_by_guardian=1"
                ).scalar_one()
            )
            tags = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM scout_opportunity_tag"
                ).scalar_one()
            )
        print(
            json.dumps(
                {
                    "migration_revision": MigrationManager().verify(runtime),
                    "status": status,
                    "suppressed": suppressed,
                    "tags": tags,
                    "guardian_required": True,
                    "events": "NOT_STARTED",
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
