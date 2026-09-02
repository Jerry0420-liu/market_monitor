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
    parser = argparse.ArgumentParser(description="M6 event and notification diagnostics")
    parser.add_argument("--data-dir", required=True, type=Path)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(parser.parse_args().data_dir))
    try:
        MigrationManager().upgrade(runtime)
        with runtime.read_connection() as connection:
            events = {
                str(row.event_status): int(row.total)
                for row in connection.exec_driver_sql(
                    "SELECT event_status,count(*) AS total FROM current_event_projection "
                    "GROUP BY event_status ORDER BY event_status"
                )
            }
            delivery = {
                str(row.delivery_status): int(row.total)
                for row in connection.exec_driver_sql(
                    "SELECT delivery_status,count(*) AS total FROM notification_delivery_state "
                    "GROUP BY delivery_status ORDER BY delivery_status"
                )
            }
            attempts = int(
                connection.exec_driver_sql("SELECT count(*) FROM delivery_attempt").scalar_one()
            )
            generation = int(
                connection.exec_driver_sql(
                    "SELECT value FROM system_metadata WHERE key='restore_generation'"
                ).scalar_one()
            )
        print(
            json.dumps(
                {
                    "migration_revision": MigrationManager().verify(runtime),
                    "events": events,
                    "delivery": delivery,
                    "attempts": attempts,
                    "restore_generation": generation,
                    "webhook": "DISABLED_BY_DEFAULT",
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
