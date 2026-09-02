from collections.abc import Iterator
from pathlib import Path

import pytest
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


@pytest.fixture
def m2_runtime(tmp_path: Path) -> Iterator[tuple[DatabaseRuntime, WriterQueue, ArtifactStore]]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    try:
        yield runtime, writer, ArtifactStore(runtime, writer)
    finally:
        writer.close()
        runtime.close()
