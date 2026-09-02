from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_persistence.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    StoredArtifact,
)
from market_monitor_persistence.writer import TransactionContext

from tests.m9.conftest import M9Runtime


def test_artifact_registration_revalidates_file_inside_writer_command(
    m9_runtime: M9Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = m9_runtime.artifacts.put_bytes(b"registration race", "application/octet-stream")
    entered = Event()
    release = Event()

    def blocker(_: TransactionContext) -> None:
        entered.set()
        assert release.wait(timeout=5)

    running = m9_runtime.writer.submit(blocker)
    assert entered.wait(timeout=5)
    original_verify = ArtifactStore._verify_path
    verified_before_enqueue = Event()
    calls = 0

    def observe_verify(path: Path, digest: str) -> None:
        nonlocal calls
        calls += 1
        original_verify(path, digest)
        if calls == 1:
            verified_before_enqueue.set()

    monkeypatch.setattr(ArtifactStore, "_verify_path", staticmethod(observe_verify))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            registration = executor.submit(m9_runtime.artifacts.register, artifact)
            assert verified_before_enqueue.wait(timeout=5)
            m9_runtime.artifacts.path_for(artifact.sha256).unlink()
            release.set()
            running.result(timeout=5)
            with pytest.raises(ArtifactNotFoundError):
                registration.result(timeout=5)
    finally:
        release.set()
    with m9_runtime.runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM artifact_object WHERE sha256=?",
                (artifact.sha256,),
            ).scalar_one()
            == 0
        )


def test_manifest_creation_revalidates_quote_inside_writer_command(
    m9_runtime: M9Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    with m9_runtime.runtime.read_connection() as connection:
        source = connection.exec_driver_sql(
            "SELECT e.epoch_uid,p.external_code FROM market_source_epoch e "
            "JOIN provider_mapping p ON p.provider_key=e.provider_key "
            "WHERE e.status='ACTIVE' ORDER BY e.started_at LIMIT 1"
        ).one()
    ingestion = IngestionService(
        m9_runtime.runtime, m9_runtime.writer, m9_runtime.artifacts
    ).ingest(
        str(source.epoch_uid),
        ProviderBatch(
            "manifest-race-batch",
            "2026-08-13T00:00:11Z",
            (
                ProviderRecord(
                    str(source.external_code),
                    "2026-08-13T00:00:10Z",
                    "10.1000",
                    101,
                    {},
                ),
            ),
        ),
    )
    with m9_runtime.runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=?",
                (ingestion.lineages[0],),
            ).scalar_one()
        )

    registered = Event()
    release = Event()
    original_register = m9_runtime.artifacts.register

    with m9_runtime.runtime.read_connection() as connection:
        manifest_count_before = int(
            connection.exec_driver_sql("SELECT count(*) FROM input_manifest").scalar_one()
        )

    def pause_after_registration(artifact: StoredArtifact) -> None:
        original_register(artifact)
        registered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(m9_runtime.artifacts, "register", pause_after_registration)
    builder = SnapshotBuilder(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        creating = executor.submit(
            builder.create_manifest,
            [quote_uid],
            datetime(2026, 8, 13, 0, 0, 30, tzinfo=UTC),
        )
        assert registered.wait(timeout=5)
        m9_runtime.writer.submit(
            lambda transaction: transaction.connection.exec_driver_sql(
                "DELETE FROM market_quote WHERE quote_uid=?",
                (quote_uid,),
            )
        ).result()
        release.set()
        with pytest.raises(LookupError):
            creating.result(timeout=5)

    with m9_runtime.runtime.read_connection() as connection:
        manifest_count_after = int(
            connection.exec_driver_sql("SELECT count(*) FROM input_manifest").scalar_one()
        )
    assert manifest_count_after == manifest_count_before
