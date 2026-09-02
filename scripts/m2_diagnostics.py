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
    parser = argparse.ArgumentParser(description="M2 reference and acquisition diagnostics")
    parser.add_argument("--data-dir", required=True, type=Path)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(parser.parse_args().data_dir))
    try:
        MigrationManager().upgrade(runtime)
        with runtime.read_connection() as connection:
            counts = {
                table: int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
                for table in (
                    "instrument",
                    "sector",
                    "market_source_epoch",
                    "market_data_batch",
                    "market_quote",
                    "capability_health_report",
                )
            }
            unreferenced = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM artifact_object a LEFT JOIN market_data_batch b "
                    "ON b.raw_artifact_sha256=a.sha256 WHERE b.batch_uid IS NULL"
                ).scalar_one()
            )
        print(
            json.dumps(
                {
                    "migration_revision": MigrationManager().verify(runtime),
                    "counts": counts,
                    "unreferenced_artifacts": unreferenced,
                    "live_provider": "DISABLED",
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
