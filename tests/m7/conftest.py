from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue

from tests.support.thresholds import activate_test_threshold_pair


@pytest.fixture
def m7_runtime(tmp_path: Path) -> Iterator[tuple[DatabaseRuntime, WriterQueue]]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        activate_test_threshold_pair(
            runtime,
            writer,
            ArtifactStore(runtime, writer),
            datetime(2026, 8, 4, tzinfo=UTC),
        )
        yield runtime, writer
    finally:
        writer.close()
        runtime.close()
