from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from market_monitor_persistence.artifacts import ArtifactError, ArtifactStore
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
_DATABASE_NAME = "database.sqlite3"
_MANIFEST_NAME = "backup-manifest.json"
_MANIFEST_DIGEST_NAME = "backup-manifest.sha256"


class BackupError(RuntimeError):
    """Raised when an online backup cannot be created and verified safely."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    sha256: str
    size_bytes: int
    created_at: str


@dataclass(frozen=True)
class BackupVerification:
    ok: bool
    integrity_check: str
    migration_revision: str | None
    sha256: str
    error: str | None = None


@dataclass(frozen=True)
class BackupSetResult:
    path: Path
    manifest_sha256: str
    database_sha256: str
    artifact_count: int
    size_bytes: int
    created_at: str


@dataclass(frozen=True)
class BackupSetVerification:
    ok: bool
    error_code: str | None
    manifest_sha256: str
    database_sha256: str
    artifact_count: int
    size_bytes: int
    migration_revision: str | None


@dataclass(frozen=True)
class _BackupArtifact:
    sha256: str
    size_bytes: int
    media_type: str
    relative_path: str


def create_online_backup(
    runtime: DatabaseRuntime, writer: WriterQueue, destination: Path
) -> BackupResult:
    destination = destination.resolve()
    if destination == runtime.paths.database_file.resolve():
        raise BackupError("backup destination must differ from the source database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{new_uid()}.tmp")

    def command(transaction: TransactionContext) -> None:
        source = cast(sqlite3.Connection, transaction.connection.connection.driver_connection)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.execute("PRAGMA synchronous=FULL")
            target.commit()
        finally:
            target.close()

    try:
        writer.submit(command).result()
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        verification = verify_backup(temporary)
        if not verification.ok:
            raise BackupError(f"backup verification failed: {verification.error}")
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
        return BackupResult(
            path=destination,
            sha256=sha256_file(destination),
            size_bytes=destination.stat().st_size,
            created_at=format_rfc3339(utc_now()),
        )
    finally:
        temporary.unlink(missing_ok=True)


def create_backup_set(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    destination: Path,
    *,
    now: Callable[[], datetime] = utc_now,
    free_space: Callable[[Path], int] | None = None,
) -> BackupSetResult:
    """Create and atomically publish a database-and-Artifact backup set."""
    target = destination.resolve()
    source_root = runtime.paths.data_directory.resolve()
    if target == source_root or target.is_relative_to(source_root):
        raise BackupError("backup set destination must be outside the source data directory")
    if target.exists():
        raise BackupError("backup set destination already exists")

    space_reader = _available_space if free_space is None else free_space
    initial_size = _estimated_referenced_size(runtime)
    if space_reader(_nearest_existing(target.parent)) < _required_space(initial_size):
        raise BackupError("insufficient free space for backup set")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{new_uid()}.staging"
    staging.mkdir()
    database_path = staging / _DATABASE_NAME
    lease_uids: tuple[str, ...] = ()
    created_at = format_rfc3339(now())
    purpose = f"BACKUP_SET:{new_uid()}"
    try:
        backup_artifacts, lease_uids = writer.submit(
            lambda transaction: _snapshot_and_lease(
                transaction,
                database_path,
                purpose,
                created_at,
                format_rfc3339(now() + timedelta(days=1)),
            )
        ).result()
        with database_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        database_verification = verify_backup(database_path)
        if not database_verification.ok:
            raise BackupError(
                f"backup set database verification failed: {database_verification.error}"
            )
        actual_size = database_path.stat().st_size + sum(
            item.size_bytes for item in backup_artifacts
        )
        if space_reader(_nearest_existing(staging)) < _required_space(actual_size):
            raise BackupError("insufficient free space for backup set")

        manifest_artifacts: list[dict[str, object]] = []
        for item in backup_artifacts:
            expected_source = artifacts.path_for(item.sha256)
            expected_relative = expected_source.relative_to(
                runtime.paths.artifact_directory.resolve()
            ).as_posix()
            if item.relative_path != expected_relative:
                raise BackupError("artifact metadata path is invalid")
            relative = f"artifacts/{item.relative_path}"
            copied = _safe_member(staging, relative)
            copied.parent.mkdir(parents=True, exist_ok=True)
            try:
                with artifacts.open_verified(item.sha256) as source, copied.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            except (ArtifactError, OSError) as error:
                raise BackupError("artifact copy or verification failed") from error
            if copied.stat().st_size != item.size_bytes or sha256_file(copied) != item.sha256:
                raise BackupError("artifact copy or verification failed")
            _sync_directory(copied.parent)
            manifest_artifacts.append(
                {
                    "media_type": item.media_type,
                    "path": relative,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
            )

        database_sha256 = sha256_file(database_path)
        manifest: dict[str, object] = {
            "artifacts": manifest_artifacts,
            "created_at": created_at,
            "database": {
                "migration_revision": database_verification.migration_revision,
                "path": _DATABASE_NAME,
                "sha256": database_sha256,
                "size_bytes": database_path.stat().st_size,
            },
            "format_version": 1,
        }
        manifest_bytes = _canonical_json(manifest)
        manifest_sha256 = sha256_bytes(manifest_bytes)
        _write_durable(staging / _MANIFEST_NAME, manifest_bytes)
        _write_durable(staging / _MANIFEST_DIGEST_NAME, f"{manifest_sha256}\n".encode("ascii"))
        _sync_tree_directories(staging)

        verification = verify_backup_set(staging)
        if not verification.ok:
            raise BackupError(f"backup set verification failed: {verification.error_code}")
        if target.exists():
            raise BackupError("backup set destination already exists")
        try:
            os.rename(staging, target)
        except OSError as error:
            raise BackupError("backup set publish failed") from error
        try:
            _sync_directory(target.parent)
            _audit_backup_created(
                writer,
                created_at,
                manifest_sha256,
                database_sha256,
                len(backup_artifacts),
            )
        except Exception as error:
            raise BackupError(
                "backup set published but finalization failed; retain and verify the destination"
            ) from error
        return BackupSetResult(
            path=target,
            manifest_sha256=manifest_sha256,
            database_sha256=database_sha256,
            artifact_count=len(backup_artifacts),
            size_bytes=verification.size_bytes,
            created_at=created_at,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if lease_uids:
            _release_leases(writer, lease_uids)


def verify_backup_set(path: Path) -> BackupSetVerification:
    """Verify a complete backup set without changing it."""
    root = path.resolve()
    if not root.is_dir():
        return _set_failure("BACKUP_SET_MISSING")
    try:
        manifest_path = _safe_member(root, _MANIFEST_NAME)
        digest_path = _safe_member(root, _MANIFEST_DIGEST_NAME)
        if not manifest_path.is_file() or not digest_path.is_file():
            return _set_failure("MANIFEST_MISSING")
        raw = manifest_path.read_bytes()
        manifest_sha256 = sha256_bytes(raw)
        expected_manifest_sha256 = digest_path.read_text(encoding="ascii").strip()
        if _DIGEST.fullmatch(expected_manifest_sha256) is None:
            return _set_failure("MANIFEST_DIGEST_INVALID", manifest_sha256)
        if manifest_sha256 != expected_manifest_sha256:
            return _set_failure("MANIFEST_HASH_MISMATCH", manifest_sha256)
        parsed: object = json.loads(raw.decode("utf-8"))
        if raw != _canonical_json(parsed):
            return _set_failure("MANIFEST_NOT_CANONICAL", manifest_sha256)
        if not isinstance(parsed, dict) or set(parsed) != {
            "artifacts",
            "created_at",
            "database",
            "format_version",
        }:
            return _set_failure("MANIFEST_INVALID", manifest_sha256)
        if parsed.get("format_version") != 1 or not isinstance(parsed.get("created_at"), str):
            return _set_failure("MANIFEST_INVALID", manifest_sha256)

        database = parsed.get("database")
        if not isinstance(database, dict) or set(database) != {
            "migration_revision",
            "path",
            "sha256",
            "size_bytes",
        }:
            return _set_failure("MANIFEST_INVALID", manifest_sha256)
        database_relative = database.get("path")
        database_digest = database.get("sha256")
        database_size = database.get("size_bytes")
        migration_revision = database.get("migration_revision")
        if (
            database_relative != _DATABASE_NAME
            or not _valid_digest(database_digest)
            or not _valid_size(database_size)
            or not isinstance(migration_revision, str)
            or not migration_revision
        ):
            return _set_failure("MANIFEST_INVALID", manifest_sha256)
        database_path = _safe_member(root, _DATABASE_NAME)
        if not database_path.is_file():
            return _set_failure("DATABASE_MISSING", manifest_sha256)
        if database_path.stat().st_size != database_size:
            return _set_failure("DATABASE_SIZE_MISMATCH", manifest_sha256)
        if sha256_file(database_path) != database_digest:
            return _set_failure("DATABASE_HASH_MISMATCH", manifest_sha256)
        database_verification = verify_backup(database_path)
        if (
            not database_verification.ok
            or database_verification.migration_revision != migration_revision
        ):
            return _set_failure("DATABASE_INVALID", manifest_sha256)

        raw_artifacts = parsed.get("artifacts")
        if not isinstance(raw_artifacts, list):
            return _set_failure("MANIFEST_INVALID", manifest_sha256)
        seen_digests: set[str] = set()
        seen_paths: set[str] = set()
        manifest_records: list[tuple[str, int, str, str]] = []
        artifact_bytes = 0
        previous_digest = ""
        for raw_artifact in raw_artifacts:
            if not isinstance(raw_artifact, dict) or set(raw_artifact) != {
                "media_type",
                "path",
                "sha256",
                "size_bytes",
            }:
                return _set_failure("MANIFEST_INVALID", manifest_sha256)
            digest = raw_artifact.get("sha256")
            relative = raw_artifact.get("path")
            size = raw_artifact.get("size_bytes")
            media_type = raw_artifact.get("media_type")
            if (
                not _valid_digest(digest)
                or not isinstance(relative, str)
                or not _valid_size(size)
                or not isinstance(media_type, str)
                or not media_type
            ):
                return _set_failure("MANIFEST_INVALID", manifest_sha256)
            assert isinstance(digest, str)
            assert isinstance(size, int)
            expected_relative = f"artifacts/{digest[:2]}/{digest[2:4]}/{digest}"
            if relative != expected_relative:
                return _set_failure("MANIFEST_PATH_INVALID", manifest_sha256)
            if digest <= previous_digest or digest in seen_digests or relative in seen_paths:
                return _set_failure("MANIFEST_INVALID", manifest_sha256)
            previous_digest = digest
            seen_digests.add(digest)
            seen_paths.add(relative)
            artifact_path = _safe_member(root, relative)
            if not artifact_path.is_file():
                return _set_failure("ARTIFACT_MISSING", manifest_sha256)
            if artifact_path.stat().st_size != size:
                return _set_failure("ARTIFACT_SIZE_MISMATCH", manifest_sha256)
            if sha256_file(artifact_path) != digest:
                return _set_failure("ARTIFACT_HASH_MISMATCH", manifest_sha256)
            manifest_records.append((digest, size, media_type, relative))
            artifact_bytes += size
        database_records = _database_artifact_records(database_path)
        if database_records is None or manifest_records != database_records:
            return _set_failure("ARTIFACT_REFERENCE_MISMATCH", manifest_sha256)
        return BackupSetVerification(
            ok=True,
            error_code=None,
            manifest_sha256=manifest_sha256,
            database_sha256=str(database_digest),
            artifact_count=len(raw_artifacts),
            size_bytes=(
                int(database_size) + artifact_bytes + len(raw) + digest_path.stat().st_size
            ),
            migration_revision=str(migration_revision),
        )
    except (
        OSError,
        sqlite3.DatabaseError,
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return _set_failure("MANIFEST_INVALID")


def publish_backup(staged: Path, destination: Path) -> None:
    staged = staged.resolve()
    destination = destination.resolve()
    if not staged.is_file():
        raise BackupError("staged backup file is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, destination)
    _sync_directory(destination.parent)


def verify_backup(path: Path) -> BackupVerification:
    if not path.is_file():
        return BackupVerification(False, "missing", None, "", "backup file is missing")
    digest = sha256_file(path)
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = None if revision_row is None else str(revision_row[0])
        finally:
            connection.close()
        return BackupVerification(
            ok=integrity == "ok" and revision is not None,
            integrity_check=integrity,
            migration_revision=revision,
            sha256=digest,
            error=None if integrity == "ok" and revision is not None else "integrity failure",
        )
    except (OSError, sqlite3.DatabaseError) as error:
        return BackupVerification(False, "error", None, digest, type(error).__name__)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree_directories(root: Path) -> None:
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _sync_directory(directory)


def _snapshot_and_lease(
    transaction: TransactionContext,
    database_path: Path,
    purpose: str,
    created_at: str,
    valid_until: str,
) -> tuple[tuple[_BackupArtifact, ...], tuple[str, ...]]:
    source = cast(sqlite3.Connection, transaction.connection.connection.driver_connection)
    target = sqlite3.connect(database_path)
    try:
        source.backup(target)
        target.execute("PRAGMA synchronous=FULL")
        target.commit()
        rows = target.execute(
            "SELECT refs.sha256,a.size_bytes,a.media_type,a.relative_path "
            "FROM (SELECT raw_artifact_sha256 AS sha256 FROM market_data_batch "
            "UNION SELECT artifact_sha256 AS sha256 FROM input_manifest "
            "UNION SELECT report_sha256 AS sha256 FROM threshold_validation "
            "UNION SELECT evidence_sha256 AS sha256 FROM threshold_activation_evidence "
            "UNION SELECT replay_evidence_sha256 AS sha256 FROM threshold_activation_evidence "
            "UNION SELECT r.output_hash AS sha256 FROM rule_execution r "
            "JOIN artifact_object evidence ON evidence.sha256=r.output_hash) refs "
            "LEFT JOIN artifact_object a ON a.sha256=refs.sha256 ORDER BY refs.sha256"
        ).fetchall()
    finally:
        target.close()
    backup_artifacts: list[_BackupArtifact] = []
    lease_uids: list[str] = []
    for row in rows:
        if row[1] is None or row[2] is None or row[3] is None:
            raise BackupError("referenced artifact metadata is missing")
        item = _BackupArtifact(str(row[0]), int(row[1]), str(row[2]), str(row[3]))
        lease_uid = new_uid()
        transaction.connection.exec_driver_sql(
            "INSERT INTO artifact_lease(lease_uid,sha256,purpose,valid_until,created_at) "
            "VALUES (?,?,?,?,?)",
            (lease_uid, item.sha256, purpose, valid_until, created_at),
        )
        backup_artifacts.append(item)
        lease_uids.append(lease_uid)
    start_hash = _detail_hash(
        {
            "artifact_count": len(backup_artifacts),
            "operation": purpose,
        }
    )
    transaction.connection.exec_driver_sql(
        "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
        "detail_hash,created_at) VALUES (?,'BACKUP_SET_STARTED',NULL,NULL,?,?)",
        (new_uid(), start_hash, created_at),
    )
    return tuple(backup_artifacts), tuple(lease_uids)


def _release_leases(writer: WriterQueue, lease_uids: tuple[str, ...]) -> None:
    def command(transaction: TransactionContext) -> None:
        for lease_uid in lease_uids:
            transaction.connection.exec_driver_sql(
                "DELETE FROM artifact_lease WHERE lease_uid=?", (lease_uid,)
            )

    writer.submit(command).result()


def _audit_backup_created(
    writer: WriterQueue,
    created_at: str,
    manifest_sha256: str,
    database_sha256: str,
    artifact_count: int,
) -> None:
    detail_hash = _detail_hash(
        {
            "artifact_count": artifact_count,
            "database_sha256": database_sha256,
            "manifest_sha256": manifest_sha256,
        }
    )

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
            "detail_hash,created_at) VALUES (?,'BACKUP_SET_CREATED',NULL,NULL,?,?)",
            (new_uid(), detail_hash, created_at),
        )

    writer.submit(command).result()


def _estimated_referenced_size(runtime: DatabaseRuntime) -> int:
    with runtime.read_connection() as connection:
        artifact_size = int(
            connection.exec_driver_sql(
                "SELECT coalesce(sum(a.size_bytes),0) FROM artifact_object a JOIN "
                "(SELECT raw_artifact_sha256 AS sha256 FROM market_data_batch "
                "UNION SELECT artifact_sha256 AS sha256 FROM input_manifest "
                "UNION SELECT report_sha256 AS sha256 FROM threshold_validation "
                "UNION SELECT evidence_sha256 AS sha256 FROM threshold_activation_evidence "
                "UNION SELECT replay_evidence_sha256 AS sha256 FROM threshold_activation_evidence "
                "UNION SELECT r.output_hash AS sha256 FROM rule_execution r "
                "JOIN artifact_object evidence ON evidence.sha256=r.output_hash) refs "
                "ON refs.sha256=a.sha256"
            ).scalar_one()
        )
    return runtime.paths.database_file.stat().st_size + artifact_size


def _database_artifact_records(
    database_path: Path,
) -> list[tuple[str, int, str, str]] | None:
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT refs.sha256,a.size_bytes,a.media_type,a.relative_path "
            "FROM (SELECT raw_artifact_sha256 AS sha256 FROM market_data_batch "
            "UNION SELECT artifact_sha256 AS sha256 FROM input_manifest "
            "UNION SELECT report_sha256 AS sha256 FROM threshold_validation "
            "UNION SELECT evidence_sha256 AS sha256 FROM threshold_activation_evidence "
            "UNION SELECT replay_evidence_sha256 AS sha256 FROM threshold_activation_evidence "
            "UNION SELECT r.output_hash AS sha256 FROM rule_execution r "
            "JOIN artifact_object evidence ON evidence.sha256=r.output_hash) refs "
            "LEFT JOIN artifact_object a ON a.sha256=refs.sha256 ORDER BY refs.sha256"
        ).fetchall()
    finally:
        connection.close()
    if any(value is None for row in rows for value in row):
        return None
    return [
        (str(digest), int(size), str(media_type), f"artifacts/{relative_path}")
        for digest, size, media_type, relative_path in rows
    ]


def _required_space(content_size: int) -> int:
    return content_size + max(16 * 1024 * 1024, content_size // 10)


def _available_space(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _nearest_existing(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise BackupError("backup set destination has no accessible parent")
        candidate = parent
    if not candidate.is_dir():
        candidate = candidate.parent
    return candidate


def _safe_member(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise ValueError("invalid backup set member path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid backup set member path")
    candidate = (root / Path(*parts)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("backup set member path escapes its root")
    return candidate


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _detail_hash(value: object) -> str:
    return sha256_bytes(_canonical_json(value))


def _write_durable(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_size(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _set_failure(
    error_code: str, manifest_sha256: str = "", database_sha256: str = ""
) -> BackupSetVerification:
    return BackupSetVerification(
        ok=False,
        error_code=error_code,
        manifest_sha256=manifest_sha256,
        database_sha256=database_sha256,
        artifact_count=0,
        size_bytes=0,
        migration_revision=None,
    )
