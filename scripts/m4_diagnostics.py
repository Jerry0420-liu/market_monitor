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
    parser = argparse.ArgumentParser(description="M4 Guardian diagnostics")
    parser.add_argument("--data-dir", required=True, type=Path)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(parser.parse_args().data_dir))
    try:
        MigrationManager().upgrade(runtime)
        with runtime.read_connection() as connection:
            effects = {
                str(row.guardian_effect): int(row.total)
                for row in connection.exec_driver_sql(
                    "SELECT guardian_effect,count(*) AS total FROM guardian_evaluation "
                    "GROUP BY guardian_effect ORDER BY guardian_effect"
                )
            }
            blocking = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM guardian_evaluation WHERE blocking=1"
                ).scalar_one()
            )
            risks = int(
                connection.exec_driver_sql("SELECT count(*) FROM guardian_risk_tag").scalar_one()
            )
        print(
            json.dumps(
                {
                    "migration_revision": MigrationManager().verify(runtime),
                    "effects": effects,
                    "blocking": blocking,
                    "risks": risks,
                    "t1_protection": "MANDATORY",
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
