import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from market_monitor_persistence.artifacts import (
    ArtifactIntegrityError,
    ArtifactMetadataConflictError,
    ArtifactNotFoundError,
    ArtifactPathError,
    ArtifactStore,
)
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue, WriterQueueClosedError


def _store(tmp_path: Path) -> tuple[DatabaseRuntime, WriterQueue, ArtifactStore]:
    runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(tmp_path))
    MigrationManager().upgrade(runtime)
    writer = WriterQueue(runtime)
    writer.start()
    return runtime, writer, ArtifactStore(runtime, writer)


def test_content_addressed_write_is_idempotent_and_verified(tmp_path: Path) -> None:
    runtime, writer, store = _store(tmp_path)
    try:
        first = store.put_bytes(b"payload", "application/octet-stream")
        second = store.put_bytes(b"payload", "application/octet-stream")
        store.register(first)
        store.register(second)

        assert first == second
        assert first.relative_path == f"{first.sha256[:2]}/{first.sha256[2:4]}/{first.sha256}"
        with store.open_verified(first.sha256) as stream:
            assert stream.read() == b"payload"
        assert store.registered_count() == 1
    finally:
        writer.close()
        runtime.close()


@pytest.mark.parametrize("digest", ["../escape", "a" * 63, "G" * 64])
def test_invalid_digest_cannot_escape_artifact_root(tmp_path: Path, digest: str) -> None:
    runtime, writer, store = _store(tmp_path)
    try:
        with pytest.raises(ArtifactPathError):
            store.path_for(digest)
    finally:
        writer.close()
        runtime.close()


def test_existing_corrupt_content_is_rejected(tmp_path: Path) -> None:
    runtime, writer, store = _store(tmp_path)
    try:
        artifact = store.put_bytes(b"expected", "text/plain")
        store.path_for(artifact.sha256).write_bytes(b"corrupt")

        with pytest.raises(ArtifactIntegrityError, match="hash"):
            store.put_bytes(b"expected", "text/plain")
    finally:
        writer.close()
        runtime.close()


def test_registration_failure_leaves_detectable_orphan_after_grace(tmp_path: Path) -> None:
    runtime, writer, store = _store(tmp_path)
    try:
        artifact = store.put_bytes(b"orphan", "application/octet-stream")
        path = store.path_for(artifact.sha256)
        old = datetime.now(UTC) - timedelta(hours=2)
        os.utime(path, (old.timestamp(), old.timestamp()))
        writer.close()
        with pytest.raises(WriterQueueClosedError):
            store.register(artifact)
        assert store.find_orphans(timedelta(hours=1), now=datetime.now(UTC)) == [path]
    finally:
        writer.close()
        runtime.close()


def test_partial_temporary_file_is_never_a_committed_orphan(tmp_path: Path) -> None:
    runtime, writer, store = _store(tmp_path)
    try:
        temporary = runtime.paths.artifact_directory / ".tmp" / "partial"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(b"partial")
        old = datetime.now(UTC) - timedelta(days=1)
        os.utime(temporary, (old.timestamp(), old.timestamp()))

        assert store.find_orphans(timedelta(seconds=0), now=datetime.now(UTC)) == []
    finally:
        writer.close()
        runtime.close()


def test_lease_lifecycle_protects_registered_artifact(tmp_path: Path) -> None:
    runtime, writer, store = _store(tmp_path)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    try:
        artifact = store.put_bytes(b"leased", "application/octet-stream")
        store.register(artifact)
        lease_uid = store.acquire_lease(
            artifact.sha256, "online-backup", now + timedelta(minutes=5), now=now
        )

        assert store.has_active_lease(artifact.sha256, now + timedelta(minutes=1))
        store.release_lease(lease_uid)
        assert not store.has_active_lease(artifact.sha256, now + timedelta(minutes=1))
    finally:
        writer.close()
        runtime.close()


def test_missing_registered_file_and_metadata_conflict_are_explicit(tmp_path: Path) -> None:
    runtime, writer, store = _store(tmp_path)
    try:
        artifact = store.put_bytes(b"same", "text/plain")
        store.register(artifact)
        conflicting = artifact.with_media_type("application/octet-stream")
        with pytest.raises(ArtifactMetadataConflictError):
            store.register(conflicting)

        store.path_for(artifact.sha256).unlink()
        with pytest.raises(ArtifactNotFoundError):
            with store.open_verified(artifact.sha256):
                pass
    finally:
        writer.close()
        runtime.close()
