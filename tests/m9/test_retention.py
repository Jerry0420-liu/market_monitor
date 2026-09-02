from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import cast

import pytest
from market_monitor_analysis.canonical import canonical_bytes, canonical_hash
from market_monitor_analysis.facts import FactExecutor
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_data.ingestion import IngestionService
from market_monitor_data.models import ProviderBatch, ProviderRecord
from market_monitor_persistence.artifacts import StoredArtifact
from market_monitor_persistence.values import new_uid, sha256_bytes

from tests.m9.conftest import M9Runtime

CUTOFF = datetime(2026, 8, 18, tzinfo=UTC)
NOW = datetime(2026, 8, 20, tzinfo=UTC)
OLD_ARTIFACT_TIME = "2026-08-01T00:00:00.000000Z"


@dataclass(frozen=True)
class QuoteRef:
    quote_uid: str
    lineage_uid: str
    batch_uid: str


@dataclass(frozen=True)
class RetentionDataset:
    protected_quote_uids: frozenset[str]
    expired_candidate: QuoteRef
    fresh_quote: QuoteRef
    unleased_artifact: StoredArtifact
    leased_artifact: StoredArtifact


def _maintenance() -> ModuleType:
    return importlib.import_module("market_monitor_persistence.maintenance")


def _source(m9_runtime: M9Runtime) -> tuple[str, str]:
    with m9_runtime.runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT e.epoch_uid,p.external_code FROM market_source_epoch e "
            "JOIN provider_mapping p ON p.provider_key=e.provider_key "
            "WHERE e.status='ACTIVE' ORDER BY e.started_at LIMIT 1"
        ).one()
    return str(row.epoch_uid), str(row.external_code)


def _ingest_quote(
    m9_runtime: M9Runtime,
    batch_id: str,
    received_at: str,
    source_time: str,
) -> QuoteRef:
    epoch_uid, external_code = _source(m9_runtime)
    result = IngestionService(m9_runtime.runtime, m9_runtime.writer, m9_runtime.artifacts).ingest(
        epoch_uid,
        ProviderBatch(
            batch_id,
            received_at,
            (ProviderRecord(external_code, source_time, "10.1000", 101, {}),),
        ),
    )
    assert len(result.lineages) == 1
    with m9_runtime.runtime.read_connection() as connection:
        quote_uid = str(
            connection.exec_driver_sql(
                "SELECT quote_uid FROM market_quote WHERE lineage_uid=? ORDER BY record_version",
                (result.lineages[0],),
            ).scalar_one()
        )
    return QuoteRef(quote_uid, result.lineages[0], result.batch_uid)


def _correct_quote(m9_runtime: M9Runtime, quote: QuoteRef, received_at: str) -> str:
    return IngestionService(
        m9_runtime.runtime, m9_runtime.writer, m9_runtime.artifacts
    ).correct_quote(
        quote.lineage_uid,
        quote.batch_uid,
        0,
        "10.2000",
        102,
        received_at,
    )


def _register_old_orphan(m9_runtime: M9Runtime, body: bytes) -> StoredArtifact:
    artifact = replace(
        m9_runtime.artifacts.put_bytes(body, "application/octet-stream"),
        created_at=OLD_ARTIFACT_TIME,
    )
    m9_runtime.artifacts.register(artifact)
    return artifact


def _add_fact_evidence(m9_runtime: M9Runtime, quote_uid: str) -> None:
    with m9_runtime.runtime.read_connection() as connection:
        subject_uid = str(
            connection.exec_driver_sql(
                "SELECT subject_uid FROM analysis_subject WHERE subject_kind='MARKET'"
            ).scalar_one()
        )
        manifest_uid = str(
            connection.exec_driver_sql(
                "SELECT manifest_uid FROM input_manifest ORDER BY created_at LIMIT 1"
            ).scalar_one()
        )
        bundle_uid = str(
            connection.exec_driver_sql(
                "SELECT bundle_uid FROM reference_version_bundle ORDER BY created_at LIMIT 1"
            ).scalar_one()
        )
    snapshots = SnapshotBuilder(m9_runtime.runtime, m9_runtime.writer, m9_runtime.artifacts)
    snapshot_uid = snapshots.create_snapshot(
        subject_uid,
        manifest_uid,
        bundle_uid,
        "OFFICIAL",
        ["QUOTES"],
        [],
        max_skew_ms=1_000,
    )
    snapshots.seal(snapshot_uid)
    fact_uid = (
        FactExecutor(m9_runtime.runtime, m9_runtime.writer)
        .execute(snapshot_uid, "OBJECTIVE_QUOTE_SUMMARY", "1")[0]
        .fact_uid
    )
    m9_runtime.writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "INSERT INTO fact_specific_evidence(fact_uid,quote_uid,evidence_role) "
            "VALUES (?,?,'KEY_INPUT')",
            (fact_uid, quote_uid),
        )
    ).result()


def _retention_dataset(m9_runtime: M9Runtime) -> RetentionDataset:
    with m9_runtime.runtime.read_connection() as connection:
        manifest_quote = connection.exec_driver_sql(
            "SELECT q.quote_uid,q.lineage_uid,q.batch_uid FROM market_quote q "
            "ORDER BY q.received_at LIMIT 1"
        ).one()
    manifest_ref = QuoteRef(
        str(manifest_quote.quote_uid),
        str(manifest_quote.lineage_uid),
        str(manifest_quote.batch_uid),
    )
    manifest_correction = _correct_quote(m9_runtime, manifest_ref, "2026-08-13T00:01:00Z")

    correction = _ingest_quote(
        m9_runtime,
        "retention-correction",
        "2026-08-13T02:00:01Z",
        "2026-08-13T02:00:00Z",
    )
    correction_version = _correct_quote(m9_runtime, correction, "2026-08-14T00:00:01Z")

    quality = _ingest_quote(
        m9_runtime,
        "retention-quality",
        "2026-08-14T00:00:01Z",
        "not-an-rfc3339-time",
    )
    quality_version = _correct_quote(m9_runtime, quality, "2026-08-15T00:00:01Z")

    fact = _ingest_quote(
        m9_runtime,
        "retention-fact",
        "2026-08-15T00:00:01Z",
        "2026-08-15T01:30:00Z",
    )
    _add_fact_evidence(m9_runtime, fact.quote_uid)
    fact_version = _correct_quote(m9_runtime, fact, "2026-08-16T00:00:00Z")

    # Received before the cutoff but sourced after it: this must still be eligible.
    candidate = _ingest_quote(
        m9_runtime,
        "retention-expired",
        "2026-08-16T00:00:01Z",
        "2026-08-19T01:30:00Z",
    )
    # Sourced before the cutoff but received after it: this must remain hot.
    fresh = _ingest_quote(
        m9_runtime,
        "retention-fresh",
        "2026-08-19T00:00:01Z",
        "2026-08-17T01:30:00Z",
    )

    unleased = _register_old_orphan(m9_runtime, b"unleased retention orphan")
    leased = _register_old_orphan(m9_runtime, b"leased retention orphan")
    m9_runtime.artifacts.acquire_lease(
        leased.sha256,
        "RETENTION_TEST",
        NOW + timedelta(days=1),
        now=NOW,
    )
    return RetentionDataset(
        frozenset(
            {
                manifest_ref.quote_uid,
                manifest_correction,
                correction.quote_uid,
                correction_version,
                quality.quote_uid,
                quality_version,
                fact.quote_uid,
                fact_version,
            }
        ),
        candidate,
        fresh,
        unleased,
        leased,
    )


def _state_fingerprint(m9_runtime: M9Runtime) -> tuple[object, ...]:
    with m9_runtime.runtime.read_connection() as connection:
        table_names = tuple(
            str(value)
            for value in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).scalars()
        )
        database = tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{table_name.replace(chr(34), chr(34) * 2)}" ORDER BY rowid'
                    ).all()
                ),
            )
            for table_name in table_names
        )
    files = tuple(
        (
            path.relative_to(m9_runtime.runtime.paths.artifact_directory).as_posix(),
            path.read_bytes(),
        )
        for path in sorted(m9_runtime.runtime.paths.artifact_directory.glob("*/*/*"))
        if path.is_file()
    )
    return database, files


def _insert_manifest_bytes(
    m9_runtime: M9Runtime,
    body: bytes,
    *,
    document_hash: str | None = None,
) -> str:
    artifact = m9_runtime.artifacts.put_bytes(
        body, "application/vnd.market-monitor.input-manifest+json"
    )
    m9_runtime.artifacts.register(artifact)
    manifest_uid = new_uid()
    m9_runtime.writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "INSERT INTO input_manifest"
            "(manifest_uid,canonical_hash,artifact_sha256,as_of_time,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                manifest_uid,
                sha256_bytes(body) if document_hash is None else document_hash,
                artifact.sha256,
                "2026-08-13T00:00:30.000000Z",
                "2026-08-13T00:00:30.000000Z",
            ),
        )
    ).result()
    return artifact.sha256


def _quote_uids(plan: object) -> tuple[str, ...]:
    return cast(tuple[str, ...], getattr(plan, "quote_uids"))


def _artifact_sha256s(plan: object) -> tuple[str, ...]:
    return cast(tuple[str, ...], getattr(plan, "artifact_sha256s"))


def test_plan_retention_parses_manifests_and_closes_every_evidence_lineage_without_mutation(
    m9_runtime: M9Runtime,
) -> None:
    dataset = _retention_dataset(m9_runtime)
    maintenance = _maintenance()
    before = _state_fingerprint(m9_runtime)

    plan = maintenance.plan_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=lambda: NOW,
    )
    repeated = maintenance.plan_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=lambda: NOW,
    )

    assert _state_fingerprint(m9_runtime) == before
    assert _quote_uids(plan) == (dataset.expired_candidate.quote_uid,)
    assert _artifact_sha256s(plan) == (dataset.unleased_artifact.sha256,)
    assert dataset.protected_quote_uids.isdisjoint(_quote_uids(plan))
    assert dataset.fresh_quote.quote_uid not in _quote_uids(plan)
    assert dataset.leased_artifact.sha256 not in _artifact_sha256s(plan)
    assert plan.candidate_count == 2
    assert len(plan.candidate_hash) == 64
    assert set(plan.candidate_hash) <= set("0123456789abcdef")
    assert repeated.candidate_hash == plan.candidate_hash


def test_apply_retention_deletes_only_the_fenced_plan_and_audits_its_hash(
    m9_runtime: M9Runtime,
) -> None:
    dataset = _retention_dataset(m9_runtime)
    maintenance = _maintenance()
    plan = maintenance.plan_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=lambda: NOW,
    )

    maintenance.apply_retention(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        plan,
        now=lambda: NOW,
    )

    with m9_runtime.runtime.read_connection() as connection:
        remaining_quotes = {
            str(value)
            for value in connection.exec_driver_sql("SELECT quote_uid FROM market_quote").scalars()
        }
        remaining_artifacts = {
            str(value)
            for value in connection.exec_driver_sql("SELECT sha256 FROM artifact_object").scalars()
        }
        audit = connection.exec_driver_sql(
            "SELECT detail_hash FROM audit_record WHERE action='RETENTION_APPLIED' "
            "ORDER BY rowid DESC LIMIT 1"
        ).one()
        lineage_count = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM quote_lineage WHERE lineage_uid=?",
                (dataset.expired_candidate.lineage_uid,),
            ).scalar_one()
        )
        raw_count = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM raw_market_record WHERE batch_uid=?",
                (dataset.expired_candidate.batch_uid,),
            ).scalar_one()
        )
    assert dataset.expired_candidate.quote_uid not in remaining_quotes
    assert dataset.protected_quote_uids <= remaining_quotes
    assert dataset.fresh_quote.quote_uid in remaining_quotes
    assert dataset.unleased_artifact.sha256 not in remaining_artifacts
    assert not m9_runtime.artifacts.path_for(dataset.unleased_artifact.sha256).exists()
    assert dataset.leased_artifact.sha256 in remaining_artifacts
    assert m9_runtime.artifacts.path_for(dataset.leased_artifact.sha256).is_file()
    assert lineage_count == 1
    assert raw_count == 1
    assert str(audit.detail_hash) == plan.candidate_hash


@pytest.mark.parametrize(
    "failure_kind",
    ["malformed", "missing", "corrupt", "noncanonical", "unknown_quote"],
)
def test_manifest_failure_is_fail_closed_and_leaves_no_mutation(
    m9_runtime: M9Runtime,
    failure_kind: str,
) -> None:
    candidate = _ingest_quote(
        m9_runtime,
        f"retention-bad-manifest-{failure_kind}",
        "2026-08-16T00:00:01Z",
        "2026-08-19T01:30:00Z",
    )
    if failure_kind in {"missing", "corrupt"}:
        with m9_runtime.runtime.read_connection() as connection:
            digest = str(
                connection.exec_driver_sql(
                    "SELECT artifact_sha256 FROM input_manifest ORDER BY created_at LIMIT 1"
                ).scalar_one()
            )
        path = m9_runtime.artifacts.path_for(digest)
        if failure_kind == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt manifest bytes")
    elif failure_kind == "malformed":
        _insert_manifest_bytes(m9_runtime, b"{")
    elif failure_kind == "noncanonical":
        document = {
            "as_of_time": "2026-08-13T00:00:30.000000Z",
            "quote_uids": [candidate.quote_uid],
        }
        noncanonical = json.dumps(document, indent=2).encode()
        assert noncanonical != canonical_bytes(document)
        _insert_manifest_bytes(
            m9_runtime,
            noncanonical,
            document_hash=canonical_hash(document),
        )
    else:
        document = {
            "as_of_time": "2026-08-13T00:00:30.000000Z",
            "quote_uids": ["unknown-retention-quote"],
        }
        _insert_manifest_bytes(
            m9_runtime,
            canonical_bytes(document),
            document_hash=canonical_hash(document),
        )
    maintenance = _maintenance()
    before = _state_fingerprint(m9_runtime)

    with pytest.raises(maintenance.RetentionError):
        maintenance.plan_retention(
            m9_runtime.runtime,
            m9_runtime.artifacts,
            CUTOFF,
            now=lambda: NOW,
        )

    assert _state_fingerprint(m9_runtime) == before


def test_apply_retention_rejects_same_count_different_candidate_set_aba(
    m9_runtime: M9Runtime,
) -> None:
    first = _ingest_quote(
        m9_runtime,
        "retention-aba-first",
        "2026-08-16T00:00:01Z",
        "2026-08-19T01:30:00Z",
    )
    maintenance = _maintenance()
    original = maintenance.plan_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=lambda: NOW,
    )
    SnapshotBuilder(m9_runtime.runtime, m9_runtime.writer, m9_runtime.artifacts).create_manifest(
        [first.quote_uid], datetime(2026, 8, 19, tzinfo=UTC)
    )
    second = _ingest_quote(
        m9_runtime,
        "retention-aba-second",
        "2026-08-17T00:00:01Z",
        "2026-08-20T01:30:00Z",
    )
    current = maintenance.plan_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=lambda: NOW,
    )
    assert original.candidate_count == current.candidate_count == 1
    assert _quote_uids(original) == (first.quote_uid,)
    assert _quote_uids(current) == (second.quote_uid,)
    assert original.candidate_hash != current.candidate_hash
    before = _state_fingerprint(m9_runtime)

    with pytest.raises(maintenance.RetentionError):
        maintenance.apply_retention(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            original,
            now=lambda: NOW,
        )

    assert _state_fingerprint(m9_runtime) == before


def test_apply_retention_rechecks_an_artifact_lease_acquired_after_planning(
    m9_runtime: M9Runtime,
) -> None:
    artifact = _register_old_orphan(m9_runtime, b"lease acquired after retention plan")
    maintenance = _maintenance()
    plan = maintenance.plan_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=lambda: NOW,
    )
    assert _artifact_sha256s(plan) == (artifact.sha256,)
    m9_runtime.artifacts.acquire_lease(
        artifact.sha256,
        "CONCURRENT_RETENTION_PROTECTION",
        NOW + timedelta(hours=1),
        now=NOW,
    )
    before = _state_fingerprint(m9_runtime)

    with pytest.raises(maintenance.RetentionError):
        maintenance.apply_retention(
            m9_runtime.runtime,
            m9_runtime.writer,
            m9_runtime.artifacts,
            plan,
            now=lambda: NOW,
        )

    assert _state_fingerprint(m9_runtime) == before
    assert m9_runtime.artifacts.path_for(artifact.sha256).is_file()
