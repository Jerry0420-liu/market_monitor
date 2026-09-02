from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import (
    format_rfc3339,
    new_uid,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from market_monitor_persistence.writer import TransactionContext, WriterQueue

_DIGEST = re.compile(r"[0-9a-f]{64}")


class ArtifactError(RuntimeError):
    """Base error for durable artifact operations."""


class ArtifactPathError(ArtifactError):
    """Raised when a digest cannot map safely below the artifact root."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when bytes do not match their content address."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when registered or requested content is absent."""


class ArtifactMetadataConflictError(ArtifactError):
    """Raised when one digest is registered with conflicting metadata."""


@dataclass(frozen=True)
class StoredArtifact:
    sha256: str
    size_bytes: int
    media_type: str
    relative_path: str
    created_at: str

    def with_media_type(self, media_type: str) -> StoredArtifact:
        return replace(self, media_type=media_type)


class ArtifactStore:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer
        self._root = runtime.paths.artifact_directory.resolve()
        self._temporary = self._root / ".tmp"
        self._temporary.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if _DIGEST.fullmatch(digest) is None:
            raise ArtifactPathError("artifact digest must be 64 lowercase hexadecimal characters")
        candidate = (self._root / digest[:2] / digest[2:4] / digest).resolve()
        if not candidate.is_relative_to(self._root):
            raise ArtifactPathError("artifact path escapes its configured root")
        return candidate

    def put_bytes(self, data: bytes, media_type: str) -> StoredArtifact:
        if not media_type.strip():
            raise ValueError("media_type is required")
        digest = sha256_bytes(data)
        target = self.path_for(digest)
        if target.exists():
            self._verify_path(target, digest)
        else:
            temporary = self._temporary / new_uid()
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if sha256_file(temporary) != digest:
                    raise ArtifactIntegrityError("temporary artifact hash mismatch")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
                _sync_directory(target.parent)
                _sync_directory(self._root)
                self._verify_path(target, digest)
            finally:
                temporary.unlink(missing_ok=True)
        created_at = format_rfc3339(datetime.fromtimestamp(target.stat().st_mtime, UTC))
        return StoredArtifact(
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
            relative_path=target.relative_to(self._root).as_posix(),
            created_at=created_at,
        )

    def register(self, artifact: StoredArtifact) -> None:
        expected_path = self.path_for(artifact.sha256)
        if artifact.relative_path != expected_path.relative_to(self._root).as_posix():
            raise ArtifactPathError("artifact metadata path does not match its digest")
        self._verify_path(expected_path, artifact.sha256)

        def command(transaction: TransactionContext) -> None:
            self._verify_path(expected_path, artifact.sha256)
            existing = transaction.connection.exec_driver_sql(
                "SELECT size_bytes,media_type,relative_path,created_at "
                "FROM artifact_object WHERE sha256=?",
                (artifact.sha256,),
            ).one_or_none()
            expected = (
                artifact.size_bytes,
                artifact.media_type,
                artifact.relative_path,
                artifact.created_at,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ArtifactMetadataConflictError(
                        f"conflicting metadata for artifact {artifact.sha256}"
                    )
                return
            transaction.connection.exec_driver_sql(
                "INSERT INTO artifact_object "
                "(sha256,size_bytes,media_type,relative_path,created_at) VALUES (?,?,?,?,?)",
                (artifact.sha256, *expected),
            )

        self._writer.submit(command).result()

    @contextmanager
    def open_verified(self, digest: str) -> Iterator[BinaryIO]:
        path = self.path_for(digest)
        if not path.is_file():
            raise ArtifactNotFoundError(f"artifact file is missing: {digest}")
        with path.open("rb") as stream:
            if _sha256_stream(stream) != digest:
                raise ArtifactIntegrityError(f"artifact hash mismatch: {digest}")
            stream.seek(0)
            yield stream

    def registered_count(self) -> int:
        with self._runtime.read_connection() as connection:
            return int(
                connection.exec_driver_sql("SELECT count(*) FROM artifact_object").scalar_one()
            )

    def acquire_lease(
        self, digest: str, purpose: str, valid_until: datetime, *, now: datetime | None = None
    ) -> str:
        self.path_for(digest)
        current = utc_now() if now is None else now
        if valid_until <= current:
            raise ValueError("artifact lease must expire in the future")
        lease_uid = new_uid()
        created_at = format_rfc3339(current)
        expires_at = format_rfc3339(valid_until)

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO artifact_lease "
                "(lease_uid,sha256,purpose,valid_until,created_at) VALUES (?,?,?,?,?)",
                (lease_uid, digest, purpose, expires_at, created_at),
            )

        self._writer.submit(command).result()
        return lease_uid

    def release_lease(self, lease_uid: str) -> None:
        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "DELETE FROM artifact_lease WHERE lease_uid=?", (lease_uid,)
            )

        self._writer.submit(command).result()

    def has_active_lease(self, digest: str, at: datetime) -> bool:
        instant = format_rfc3339(at)
        with self._runtime.read_connection() as connection:
            count = connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_lease WHERE sha256=? AND valid_until>?",
                (digest, instant),
            ).scalar_one()
        return int(count) > 0

    def find_orphans(self, grace_period: timedelta, *, now: datetime | None = None) -> list[Path]:
        if grace_period < timedelta(0):
            raise ValueError("grace_period must be non-negative")
        current = utc_now() if now is None else now
        cutoff = current.timestamp() - grace_period.total_seconds()
        with self._runtime.read_connection() as connection:
            registered = set(
                connection.exec_driver_sql("SELECT sha256 FROM artifact_object").scalars()
            )
        orphans: list[Path] = []
        for path in self._root.glob("*/*/*"):
            if not path.is_file() or _DIGEST.fullmatch(path.name) is None:
                continue
            if path != self.path_for(path.name):
                continue
            if path.name not in registered and path.stat().st_mtime <= cutoff:
                orphans.append(path)
        return sorted(orphans)

    @staticmethod
    def _verify_path(path: Path, digest: str) -> None:
        if not path.is_file():
            raise ArtifactNotFoundError(f"artifact file is missing: {digest}")
        if sha256_file(path) != digest:
            raise ArtifactIntegrityError(f"artifact hash mismatch: {digest}")


def _sha256_stream(stream: BinaryIO) -> str:
    from hashlib import sha256

    digest = sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
