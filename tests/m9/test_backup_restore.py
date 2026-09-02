import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

import market_monitor_persistence.backup as backup
import pytest
from market_monitor_persistence.values import sha256_bytes

from tests.m9.conftest import M9Runtime

NOW = datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)


def _manifest(path: Path) -> dict[str, object]:
    value: object = json.loads((path / "backup-manifest.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _rewrite_manifest(path: Path, value: dict[str, object]) -> None:
    raw = _canonical(value)
    (path / "backup-manifest.json").write_bytes(raw)
    (path / "backup-manifest.sha256").write_text(f"{sha256_bytes(raw)}\n", encoding="ascii")


def _artifact_paths(manifest: dict[str, object]) -> list[str]:
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    return [str(item["path"]) for item in artifacts if isinstance(item, dict)]


def test_backup_set_contains_snapshot_database_and_every_referenced_artifact(
    m9_runtime: M9Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "backup-sets" / "one"
    original_open = m9_runtime.artifacts.open_verified
    observed_leases: list[frozenset[str]] = []

    def observe_lease(digest: str) -> AbstractContextManager[BinaryIO]:
        with m9_runtime.runtime.read_connection() as connection:
            observed_leases.append(
                frozenset(
                    str(value)
                    for value in connection.exec_driver_sql(
                        "SELECT sha256 FROM artifact_lease WHERE purpose LIKE 'BACKUP_SET:%'"
                    ).scalars()
                )
            )
        return original_open(digest)

    monkeypatch.setattr(m9_runtime.artifacts, "open_verified", observe_lease)
    result = backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )

    manifest = _manifest(destination)
    raw = (destination / "backup-manifest.json").read_bytes()
    assert result.path == destination.resolve()
    assert result.artifact_count == len(m9_runtime.referenced_artifacts)
    assert result.manifest_sha256 == sha256_bytes(raw)
    assert raw == _canonical(manifest)
    assert (destination / "backup-manifest.sha256").read_text(encoding="ascii") == (
        f"{result.manifest_sha256}\n"
    )
    assert manifest["format_version"] == 1
    assert manifest["created_at"] == "2026-08-13T01:02:03.000000Z"
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    assert {str(item["sha256"]) for item in artifacts if isinstance(item, dict)} == set(
        m9_runtime.referenced_artifacts
    )
    assert all((destination / relative).is_file() for relative in _artifact_paths(manifest))

    database = manifest["database"]
    assert isinstance(database, dict)
    database_path = destination / str(database["path"])
    with sqlite3.connect(database_path) as sqlite_backup:
        snapshot_references = {
            str(value[0])
            for value in sqlite_backup.execute(
                "SELECT raw_artifact_sha256 FROM market_data_batch "
                "UNION SELECT artifact_sha256 FROM input_manifest "
                "UNION SELECT report_sha256 FROM threshold_validation "
                "UNION SELECT evidence_sha256 FROM threshold_activation_evidence "
                "UNION SELECT replay_evidence_sha256 FROM threshold_activation_evidence"
            )
        }
    assert snapshot_references == set(m9_runtime.referenced_artifacts)
    assert {
        str(value[0])
        for value in sqlite_backup.execute("SELECT report_sha256 FROM threshold_validation")
    } <= snapshot_references
    assert observed_leases
    assert all(value == m9_runtime.referenced_artifacts for value in observed_leases)
    with m9_runtime.runtime.read_connection() as live_connection:
        live_leases = live_connection.exec_driver_sql(
            "SELECT count(*) FROM artifact_lease WHERE purpose LIKE 'BACKUP_SET:%'"
        ).scalar_one()
        backup_audits = list(
            live_connection.exec_driver_sql(
                "SELECT action FROM audit_record WHERE action LIKE 'BACKUP_SET_%' ORDER BY rowid"
            ).scalars()
        )
    assert live_leases == 0
    assert backup_audits == ["BACKUP_SET_STARTED", "BACKUP_SET_CREATED"]
    verification = backup.verify_backup_set(destination)
    assert verification.ok is True
    assert verification.error_code is None
    assert verification.artifact_count == len(m9_runtime.referenced_artifacts)


def test_backup_set_remains_consistent_when_source_changes_after_barrier(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )
    extra = m9_runtime.artifacts.put_bytes(b"after backup", "application/octet-stream")
    m9_runtime.artifacts.register(extra)

    verification = backup.verify_backup_set(destination)
    manifest = _manifest(destination)
    manifest_artifacts = manifest["artifacts"]
    assert isinstance(manifest_artifacts, list)
    assert verification.ok is True
    assert verification.artifact_count == len(m9_runtime.referenced_artifacts)
    assert extra.sha256 not in {
        str(item["sha256"]) for item in manifest_artifacts if isinstance(item, dict)
    }


def test_backup_set_syncs_every_staging_directory_before_publish(
    m9_runtime: M9Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "backup"
    synced: list[Path] = []
    monkeypatch.setattr(backup, "_sync_directory", lambda path: synced.append(path.resolve()))

    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )

    staging_roots = [path for path in synced if path.name.endswith(".staging")]
    assert staging_roots
    staging_root = staging_roots[-1]
    staged_directories = {
        path.relative_to(destination.resolve()) for path in destination.rglob("*") if path.is_dir()
    }
    synced_staging_directories = {
        path.relative_to(staging_root)
        for path in synced
        if path != staging_root and path.is_relative_to(staging_root)
    }
    assert staged_directories <= synced_staging_directories


def test_backup_set_failure_on_missing_source_artifact_publishes_nothing_and_releases_leases(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    missing_digest = sorted(m9_runtime.referenced_artifacts)[0]
    m9_runtime.artifacts.path_for(missing_digest).unlink()
    destination = tmp_path / "backup"

    with pytest.raises(backup.BackupError, match="artifact"):
        backup.create_backup_set(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            destination,
            now=lambda: NOW,
            free_space=lambda _: 10**12,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".backup.*.staging"))
    with m9_runtime.runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_lease WHERE purpose LIKE 'BACKUP_SET:%'"
            ).scalar_one()
            == 0
        )


def test_backup_set_refuses_insufficient_space_before_database_or_destination_mutation(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    with pytest.raises(backup.BackupError, match="space"):
        backup.create_backup_set(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            destination,
            now=lambda: NOW,
            free_space=lambda _: 0,
        )
    assert not destination.exists()
    with m9_runtime.runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM artifact_lease").scalar_one() == 0


def test_backup_set_refuses_existing_destination_without_replacing_it(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("existing", encoding="utf-8")

    with pytest.raises(backup.BackupError, match="destination"):
        backup.create_backup_set(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            destination,
            now=lambda: NOW,
            free_space=lambda _: 10**12,
        )
    assert marker.read_text(encoding="utf-8") == "existing"


def test_backup_set_publish_failure_is_bounded_and_cleans_staging_and_leases(
    m9_runtime: M9Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "backup"

    def fail_publish(_: Path, __: Path) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr("market_monitor_persistence.backup.os.rename", fail_publish)
    with pytest.raises(backup.BackupError, match="publish"):
        backup.create_backup_set(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            destination,
            now=lambda: NOW,
            free_space=lambda _: 10**12,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".backup.*.staging"))
    with m9_runtime.runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_lease WHERE purpose LIKE 'BACKUP_SET:%'"
            ).scalar_one()
            == 0
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='BACKUP_SET_CREATED'"
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("failure", ["directory-sync", "created-audit"])
def test_backup_set_reports_published_but_incomplete_finalization_without_deleting_set(
    m9_runtime: M9Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    destination = tmp_path / "backup"
    if failure == "directory-sync":
        original_sync = backup._sync_directory

        def fail_final_sync(path: Path) -> None:
            if path.resolve() == destination.parent.resolve() and destination.exists():
                raise OSError("injected final directory sync failure")
            original_sync(path)

        monkeypatch.setattr(backup, "_sync_directory", fail_final_sync)
    else:
        monkeypatch.setattr(
            backup,
            "_audit_backup_created",
            lambda *_: (_ for _ in ()).throw(RuntimeError("injected audit failure")),
        )

    with pytest.raises(backup.BackupError, match="published.*finalization"):
        backup.create_backup_set(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            destination,
            now=lambda: NOW,
            free_space=lambda _: 10**12,
        )

    assert backup.verify_backup_set(destination).ok is True
    with m9_runtime.runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_lease WHERE purpose LIKE 'BACKUP_SET:%'"
            ).scalar_one()
            == 0
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='BACKUP_SET_CREATED'"
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (("artifact", "ARTIFACT_HASH_MISMATCH"), ("database", "DATABASE_HASH_MISMATCH")),
)
def test_backup_set_verification_detects_corrupt_content(
    m9_runtime: M9Runtime,
    tmp_path: Path,
    mutation: str,
    error_code: str,
) -> None:
    destination = tmp_path / "backup"
    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )
    manifest = _manifest(destination)
    if mutation == "artifact":
        artifact_path = destination / _artifact_paths(manifest)[0]
        content = artifact_path.read_bytes()
        artifact_path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    else:
        database = manifest["database"]
        assert isinstance(database, dict)
        database_path = destination / str(database["path"])
        content = database_path.read_bytes()
        database_path.write_bytes(bytes([content[0] ^ 1]) + content[1:])

    verification = backup.verify_backup_set(destination)
    assert verification.ok is False
    assert verification.error_code == error_code


def test_backup_set_verification_rejects_manifest_path_escape_even_with_updated_digest(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )
    manifest = _manifest(destination)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list) and isinstance(artifacts[0], dict)
    artifacts[0]["path"] = "../outside"
    _rewrite_manifest(destination, manifest)

    verification = backup.verify_backup_set(destination)
    assert verification.ok is False
    assert verification.error_code == "MANIFEST_PATH_INVALID"


def test_backup_set_verification_rejects_manifest_that_omits_database_reference(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )
    manifest = _manifest(destination)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list) and len(artifacts) == len(m9_runtime.referenced_artifacts)
    removed = artifacts.pop()
    assert isinstance(removed, dict)
    (destination / str(removed["path"])).unlink()
    _rewrite_manifest(destination, manifest)

    verification = backup.verify_backup_set(destination)
    assert verification.ok is False
    assert verification.error_code == "ARTIFACT_REFERENCE_MISMATCH"


def test_backup_set_verification_rejects_unreferenced_manifest_artifact(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )
    manifest = _manifest(destination)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    content = b"not referenced by the backup database"
    digest = sha256_bytes(content)
    relative = f"artifacts/{digest[:2]}/{digest[2:4]}/{digest}"
    path = destination / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    artifacts.append(
        {
            "media_type": "application/octet-stream",
            "path": relative,
            "sha256": digest,
            "size_bytes": len(content),
        }
    )
    artifacts.sort(key=lambda item: str(item["sha256"]) if isinstance(item, dict) else "")
    _rewrite_manifest(destination, manifest)

    verification = backup.verify_backup_set(destination)
    assert verification.ok is False
    assert verification.error_code == "ARTIFACT_REFERENCE_MISMATCH"


def test_backup_set_verification_rejects_artifact_metadata_drift_from_database(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    backup.create_backup_set(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        destination,
        now=lambda: NOW,
        free_space=lambda _: 10**12,
    )
    manifest = _manifest(destination)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list) and isinstance(artifacts[0], dict)
    artifacts[0]["media_type"] = "application/x-tampered"
    _rewrite_manifest(destination, manifest)

    verification = backup.verify_backup_set(destination)
    assert verification.ok is False
    assert verification.error_code == "ARTIFACT_REFERENCE_MISMATCH"
