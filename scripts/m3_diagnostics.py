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
    parser = argparse.ArgumentParser(description="M3 snapshot, fact, and state diagnostics")
    parser.add_argument("--data-dir", required=True, type=Path)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(parser.parse_args().data_dir))
    try:
        MigrationManager().upgrade(runtime)
        with runtime.read_connection() as connection:
            counts = {
                "draft_snapshots": int(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM evaluation_snapshot WHERE snapshot_status='DRAFT'"
                    ).scalar_one()
                ),
                "sealed_snapshots": int(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM evaluation_snapshot WHERE snapshot_status='SEALED'"
                    ).scalar_one()
                ),
                "facts": int(
                    connection.exec_driver_sql("SELECT count(*) FROM fact_record").scalar_one()
                ),
                "official_projections": int(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM current_state_projection"
                    ).scalar_one()
                ),
                "stale_audits": int(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM state_evaluation "
                        "WHERE evaluation_disposition='STALE_AUDIT'"
                    ).scalar_one()
                ),
            }
        print(
            json.dumps(
                {
                    "migration_revision": MigrationManager().verify(runtime),
                    "counts": counts,
                    "guardian": "NOT_STARTED",
                    "scout": "NOT_STARTED",
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
