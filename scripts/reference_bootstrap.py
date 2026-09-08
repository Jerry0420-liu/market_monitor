"""Bootstrap artifact-backed SSE/SZSE instrument reference data."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source in reversed(
    (
        ROOT / "packages" / "data" / "src",
        ROOT / "packages" / "persistence" / "src",
    )
):
    sys.path.insert(0, str(source))
sys.path.insert(0, str(ROOT))

from market_monitor_data.reference import ReferenceRepository  # noqa: E402
from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.writer import WriterQueue  # noqa: E402

from scripts.tdx_runner import _import_listing_reference_facts  # noqa: E402


def bootstrap(data_directory: Path, listing_reference_file: Path) -> dict[str, object]:
    """Import the owner-supplied reference artifact without contacting a provider."""
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(data_directory))
    writer = WriterQueue(runtime)
    try:
        MigrationManager().upgrade(runtime)
        MigrationManager().verify(runtime)
        writer.start()
        artifacts = ArtifactStore(runtime, writer)
        references = ReferenceRepository(runtime, writer)
        imported = _import_listing_reference_facts(
            listing_reference_file,
            references,
            artifacts,
            datetime.now(UTC),
        )
        with runtime.read_connection() as connection:
            instrument_count = int(
                connection.exec_driver_sql("SELECT count(*) FROM instrument").scalar_one()
            )
            mapping_count = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM provider_mapping "
                    "WHERE provider_key='NATIVE_TDX' AND entity_kind='INSTRUMENT' "
                    "AND mapping_status='RESOLVED'"
                ).scalar_one()
            )
        return {
            **imported,
            "instrument_count": instrument_count,
            "provider_mapping_count": mapping_count,
        }
    finally:
        writer.close()
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import an approved listing-reference artifact into a runtime database"
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--listing-reference-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    print(
        json.dumps(bootstrap(arguments.data_dir, arguments.listing_reference_file), sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
