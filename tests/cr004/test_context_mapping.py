"""CR-004 versioned sector context mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import TransactionContext, WriterQueue


def test_owner_approved_context_mapping_is_bundle_pinned_and_immutable(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    """Break if runtime context can come from an unversioned or mutable relationship."""
    runtime, writer, artifacts = m3_runtime
    now = datetime(2026, 8, 24, tzinfo=UTC)
    references = ReferenceRepository(runtime, writer)
    sector_uid = references.create_sector("CONCEPT", now)
    etf_uid = references.create_instrument("ETF", now)
    style_uid = references.create_instrument("INDEX", now)
    source = artifacts.put_bytes(b"owner-approved-context-fixture", "application/json")
    artifacts.register(source)

    mapping = references.add_context_mapping_version(
        sector_uid,
        etf_instrument_uid=etf_uid,
        style_instrument_uid=style_uid,
        owner_approval_ref="CR-004 test approval",
        source_artifact_sha256=source.sha256,
        valid_from=now,
    )

    bundle_uid = SnapshotBuilder(runtime, writer, artifacts).create_reference_bundle(
        [("MAPPING", sector_uid, mapping.mapping_version_uid)]
    )
    assert mapping.owner_approval_ref == "CR-004 test approval"
    assert mapping.source_artifact_sha256 == source.sha256
    with runtime.read_connection() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_uid FROM reference_version_entry "
                "WHERE bundle_uid=? AND entity_kind='MAPPING' AND entity_uid=?",
                (bundle_uid, sector_uid),
            ).scalar_one()
            == mapping.mapping_version_uid
        )

    def mutate(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "UPDATE sector_context_mapping_version SET owner_approval_ref='changed' "
            "WHERE mapping_version_uid=?",
            (mapping.mapping_version_uid,),
        )

    with pytest.raises(Exception, match="immutable"):
        writer.submit(mutate).result()
