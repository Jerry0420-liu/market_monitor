from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_data.reference import AmbiguousReferenceError, ReferenceRepository


def test_versions_subjects_search_and_membership_are_stable(
    m2_runtime: tuple[Any, Any, Any],
) -> None:
    runtime, writer, _ = m2_runtime
    repository = ReferenceRepository(runtime, writer)
    stock = repository.create_instrument("STOCK", datetime(2026, 8, 4, tzinfo=UTC))
    repository.add_instrument_identity_version(
        stock, "SSE", "600000", "Old Name", "LISTED", "TRADING", datetime(2026, 8, 4, tzinfo=UTC)
    )
    repository.add_instrument_identity_version(
        stock, "SSE", "600001", "New Name", "LISTED", "TRADING", datetime(2026, 8, 5, tzinfo=UTC)
    )
    sector_a = repository.create_sector("INDUSTRY", datetime(2026, 8, 4, tzinfo=UTC))
    sector_b = repository.create_sector("CONCEPT", datetime(2026, 8, 4, tzinfo=UTC))
    repository.add_sector_version(sector_a, "Shared", datetime(2026, 8, 4, tzinfo=UTC))
    repository.add_sector_version(sector_b, "Shared", datetime(2026, 8, 4, tzinfo=UTC))

    assert (
        repository.get_instrument_as_of(stock, datetime(2026, 8, 4, 12, tzinfo=UTC)).trading_code
        == "600000"
    )
    assert (
        repository.get_instrument_as_of(stock, datetime(2026, 8, 5, 12, tzinfo=UTC)).trading_code
        == "600001"
    )
    assert repository.ensure_analysis_subject(
        "INSTRUMENT", stock
    ) == repository.ensure_analysis_subject("INSTRUMENT", stock)
    assert len(repository.search_reference("Shared")) == 2
    with pytest.raises(AmbiguousReferenceError):
        repository.resolve_search("Shared")

    first = repository.freeze_membership(
        sector_a, "2026-08-04", [stock], datetime(2026, 8, 4, 1, tzinfo=UTC)
    )
    assert (
        repository.freeze_membership(
            sector_a, "2026-08-04", [stock], datetime(2026, 8, 4, 2, tzinfo=UTC)
        )
        == first
    )
    corrected = repository.freeze_membership(
        sector_a, "2026-08-04", [], datetime(2026, 8, 4, 3, tzinfo=UTC), correction=True
    )
    assert corrected != first
    assert repository.membership_as_of(sector_a, "2026-08-04", 1) == (stock,)
    assert repository.membership_as_of(sector_a, "2026-08-04", 2) == ()


def test_mapping_conflict_never_resolves_officially(m2_runtime: tuple[Any, Any, Any]) -> None:
    runtime, writer, _ = m2_runtime
    repository = ReferenceRepository(runtime, writer)
    instrument = repository.create_instrument("ETF", datetime(2026, 8, 4, tzinfo=UTC))
    repository.record_mapping_conflict(
        "FIXTURE", "510300", "INSTRUMENT", datetime(2026, 8, 4, tzinfo=UTC)
    )
    assert (
        repository.resolve_provider_mapping(
            "FIXTURE", "510300", datetime(2026, 8, 4, 1, tzinfo=UTC)
        )
        is None
    )
    repository.map_instrument("FIXTURE", "510300", instrument, datetime(2026, 8, 5, tzinfo=UTC))
    assert (
        repository.resolve_provider_mapping(
            "FIXTURE", "510300", datetime(2026, 8, 5, 1, tzinfo=UTC)
        )
        == instrument
    )
