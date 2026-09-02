from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "persistence" / "src"))

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.values import format_rfc3339, utc_now  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="M7 API and OWNER security diagnostics")
    parser.add_argument("--data-dir", required=True, type=Path)
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(parser.parse_args().data_dir))
    try:
        revision = MigrationManager().verify(runtime)
        with runtime.read_connection() as connection:
            owners = int(
                connection.exec_driver_sql("SELECT count(*) FROM owner_account").scalar_one()
            )
            sessions = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM owner_session WHERE revoked_at IS NULL AND expires_at>?",
                    (format_rfc3339(utc_now()),),
                ).scalar_one()
            )
            queries = {
                str(row.query_status): int(row.total)
                for row in connection.exec_driver_sql(
                    "SELECT query_status,count(*) AS total FROM analysis_query_record "
                    "GROUP BY query_status ORDER BY query_status"
                )
            }
            setting = connection.exec_driver_sql(
                "SELECT enabled,endpoint_source,version FROM notification_setting WHERE singleton=1"
            ).one()
        print(
            json.dumps(
                {
                    "migration_revision": revision,
                    "owner_accounts": owners,
                    "active_sessions": sessions,
                    "analysis_queries": queries,
                    "notification_setting": {
                        "enabled": bool(setting.enabled),
                        "endpoint_source": str(setting.endpoint_source),
                        "version": int(setting.version),
                    },
                    "webhook_endpoint": "ENVIRONMENT_ONLY_NOT_INSPECTED",
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
