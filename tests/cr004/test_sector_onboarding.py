"""Owner-approved P0 TDX Concept/Theme sector onboarding tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_data.health import SourceEpochService
from market_monitor_data.reference import ReferenceRepository
from market_monitor_data.tdx.models import TdxBlockArtifact, TdxBlockMembership
from market_monitor_data.tdx.sectors import TdxSectorOnboarding
from market_monitor_data.tdx.storage import TdxStorage
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 8, 24, 1, 35, tzinfo=UTC)


def test_concept_and_theme_onboarding_preserves_source_lineage_and_primary_members(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if P0 merges Theme into Concept or admits ETF members."""
    runtime, writer, artifacts = m3_runtime
    references = ReferenceRepository(runtime, writer)
    concept_stock = _register(references, "STOCK", 1, "600000")
    theme_stock = _register(references, "STOCK", 0, "300001")
    _register(references, "STOCK", 1, "600002")
    _register(references, "ETF", 1, "510300")
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "sector-1", NOW)
    storage = TdxStorage(runtime, writer)
    first_block_version = _record_blocks(
        storage,
        artifacts,
        epoch_uid,
        b"first block",
        (
            TdxBlockMembership("AI", 1, "ConceptMembership", 1, "600000"),
            TdxBlockMembership("AI", 1, "ConceptMembership", 1, "510300"),
            TdxBlockMembership("AI", 1, "ThemeMembership", 0, "300001"),
            TdxBlockMembership("AI Index", 1, "IndexMembership", 1, "600002"),
        ),
        NOW,
    )

    onboarding = TdxSectorOnboarding(runtime, references)
    first_versions = onboarding.synchronize(epoch_uid, NOW)

    assert len(first_versions) == 2
    assert onboarding.synchronize(epoch_uid, NOW) == ()
    with runtime.read_connection() as connection:
        sources = connection.exec_driver_sql(
            "SELECT membership_kind,block_version_uid FROM tdx_sector_membership_source "
            "ORDER BY membership_kind"
        ).all()
        members = connection.exec_driver_sql(
            "SELECT src.membership_kind,m.instrument_uid "
            "FROM tdx_sector_membership_source src "
            "JOIN sector_membership m ON m.membership_version_uid=src.membership_version_uid "
            "ORDER BY src.membership_kind,m.instrument_uid"
        ).all()
        subjects = connection.exec_driver_sql(
            "SELECT count(*) FROM analysis_subject WHERE subject_kind='SECTOR'"
        ).scalar_one()

    assert [(str(row.membership_kind), str(row.block_version_uid)) for row in sources] == [
        ("ConceptMembership", first_block_version),
        ("ThemeMembership", first_block_version),
    ]
    assert [(str(row.membership_kind), str(row.instrument_uid)) for row in members] == [
        ("ConceptMembership", concept_stock),
        ("ThemeMembership", theme_stock),
    ]
    assert subjects == 2


def test_new_block_artifact_creates_a_corrected_membership_version(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if a changed raw TDX artifact mutates a frozen membership in place."""
    runtime, writer, artifacts = m3_runtime
    references = ReferenceRepository(runtime, writer)
    first_stock = _register(references, "STOCK", 1, "600000")
    second_stock = _register(references, "STOCK", 1, "600001")
    storage = TdxStorage(runtime, writer)
    onboarding = TdxSectorOnboarding(runtime, references)
    first_epoch = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "sector-1", NOW)
    _record_blocks(
        storage,
        artifacts,
        first_epoch,
        b"first block",
        (TdxBlockMembership("Robotics", 1, "ConceptMembership", 1, "600000"),),
        NOW,
    )
    first_version = onboarding.synchronize(first_epoch, NOW)[0]

    later = NOW + timedelta(minutes=1)
    second_epoch = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "sector-2", later)
    second_block_version = _record_blocks(
        storage,
        artifacts,
        second_epoch,
        b"second block",
        (
            TdxBlockMembership("Robotics", 1, "ConceptMembership", 1, "600000"),
            TdxBlockMembership("Robotics", 1, "ConceptMembership", 1, "600001"),
        ),
        later,
    )

    second_version = onboarding.synchronize(second_epoch, later)[0]

    assert second_version != first_version
    with runtime.read_connection() as connection:
        source = connection.exec_driver_sql(
            "SELECT block_version_uid FROM tdx_sector_membership_source "
            "WHERE membership_version_uid=?",
            (second_version,),
        ).scalar_one()
        members = (
            connection.exec_driver_sql(
                "SELECT instrument_uid FROM sector_membership WHERE membership_version_uid=? "
                "ORDER BY instrument_uid",
                (second_version,),
            )
            .scalars()
            .all()
        )
        first_members = (
            connection.exec_driver_sql(
                "SELECT instrument_uid FROM sector_membership WHERE membership_version_uid=?",
                (first_version,),
            )
            .scalars()
            .all()
        )

    assert str(source) == second_block_version
    assert tuple(str(value) for value in members) == tuple(sorted((first_stock, second_stock)))
    assert tuple(str(value) for value in first_members) == (first_stock,)


def test_sector_onboarding_reuses_unchanged_latest_block_after_new_epoch(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if content-addressed TDX blocks vanish from sector preparation after restart."""
    runtime, writer, artifacts = m3_runtime
    references = ReferenceRepository(runtime, writer)
    stock = _register(references, "STOCK", 1, "600000")
    storage = TdxStorage(runtime, writer)
    onboarding = TdxSectorOnboarding(runtime, references)
    membership = TdxBlockMembership("Robotics", 1, "ConceptMembership", 1, "600000")
    first_epoch = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "sector-1", NOW)
    first_block_version = _record_blocks(
        storage, artifacts, first_epoch, b"same block", (membership,), NOW
    )
    assert onboarding.synchronize(first_epoch, NOW)

    later = NOW + timedelta(days=1)
    second_epoch = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "sector-2", later)
    artifact = artifacts.put_bytes(b"same block", "application/vnd.tdx.block")
    artifacts.register(artifact)
    assert (
        storage.record_block_version(
            second_epoch,
            TdxBlockArtifact(
                "block_gn.dat",
                "fixture",
                len(b"same block"),
                None,
                artifact.sha256,
                later,
                "cr004-test",
            ),
            (membership,),
        )
        is None
    )

    reused = onboarding.synchronize(second_epoch, later)

    assert len(reused) == 1
    with runtime.read_connection() as connection:
        source = connection.exec_driver_sql(
            "SELECT block_version_uid FROM tdx_sector_membership_source "
            "WHERE membership_version_uid=?",
            (reused[0],),
        ).scalar_one()
        members = (
            connection.exec_driver_sql(
                "SELECT instrument_uid FROM sector_membership WHERE membership_version_uid=?",
                (reused[0],),
            )
            .scalars()
            .all()
        )
    assert str(source) == first_block_version
    assert tuple(str(value) for value in members) == (stock,)


def test_tdx_sector_membership_source_is_append_only(
    m3_runtime: tuple[Any, Any, Any],
) -> None:
    """Fail if raw Concept/Theme lineage can be rewritten or deleted."""
    runtime, writer, artifacts = m3_runtime
    references = ReferenceRepository(runtime, writer)
    _register(references, "STOCK", 1, "600000")
    epoch_uid = SourceEpochService(runtime, writer).start_epoch("NATIVE_TDX", "sector-1", NOW)
    _record_blocks(
        TdxStorage(runtime, writer),
        artifacts,
        epoch_uid,
        b"first block",
        (TdxBlockMembership("Robotics", 1, "ConceptMembership", 1, "600000"),),
        NOW,
    )
    membership_version_uid = TdxSectorOnboarding(runtime, references).synchronize(epoch_uid, NOW)[0]

    with runtime._writer_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="append-only"):
            connection.exec_driver_sql(
                "UPDATE tdx_sector_membership_source SET block_name='rewritten' "
                "WHERE membership_version_uid=?",
                (membership_version_uid,),
            )
    with runtime._writer_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="append-only"):
            connection.exec_driver_sql(
                "DELETE FROM tdx_sector_membership_source WHERE membership_version_uid=?",
                (membership_version_uid,),
            )


def _register(
    references: ReferenceRepository,
    kind: str,
    market: int,
    code: str,
) -> str:
    instrument_uid = references.create_instrument(kind, NOW)
    references.add_instrument_identity_version(
        instrument_uid,
        "SSE" if market == 1 else "SZSE",
        code,
        code,
        "LISTED",
        "TRADING",
        NOW,
    )
    references.map_instrument("NATIVE_TDX", f"{market}:{code}", instrument_uid, NOW)
    return instrument_uid


def _record_blocks(
    storage: TdxStorage,
    artifacts: Any,
    epoch_uid: str,
    payload: bytes,
    memberships: tuple[TdxBlockMembership, ...],
    observed_at: datetime,
) -> str:
    artifact = artifacts.put_bytes(payload, "application/vnd.tdx.block")
    artifacts.register(artifact)
    version_uid = storage.record_block_version(
        epoch_uid,
        TdxBlockArtifact(
            "block_gn.dat",
            "fixture",
            len(payload),
            None,
            artifact.sha256,
            observed_at,
            "cr004-test",
        ),
        memberships,
    )
    assert version_uid is not None
    return version_uid
