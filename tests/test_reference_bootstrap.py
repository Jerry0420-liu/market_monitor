from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.reference_bootstrap import bootstrap


def _reference_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provenance": {"source_ref": "owner-approved-test-reference-fixture"},
        "facts": [
            {
                "external_code": "1:600000",
                "instrument_kind": "STOCK",
                "exchange": "SSE",
                "trading_code": "600000",
                "name": "Fixture SSE",
                "listing_status": "LISTED",
                "trading_status": "TRADING",
                "listing_effective_at": "2026-01-01T00:00:00Z",
            },
            {
                "external_code": "0:000001",
                "instrument_kind": "STOCK",
                "exchange": "SZSE",
                "trading_code": "000001",
                "name": "Fixture SZSE",
                "listing_status": "LISTED",
                "trading_status": "TRADING",
                "listing_effective_at": "2026-01-01T00:00:00Z",
            },
        ],
    }


def test_reference_bootstrap_is_idempotent_on_a_fresh_database(tmp_path: Path) -> None:
    reference_file = tmp_path / "listing-reference.json"
    reference_file.write_text(json.dumps(_reference_document()), encoding="utf-8")
    data_directory = tmp_path / "data"

    first = bootstrap(data_directory, reference_file)
    second = bootstrap(data_directory, reference_file)

    assert first["fact_count"] == 2
    assert first["new_instrument_count"] == 2
    assert first["instrument_count"] == 2
    assert first["provider_mapping_count"] == 2
    assert second["fact_count"] == 2
    assert second["new_instrument_count"] == 0
    assert second["instrument_count"] == 2
    assert second["provider_mapping_count"] == 2


def test_reference_bootstrap_requires_an_existing_artifact(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="listing Reference fact file does not exist"):
        bootstrap(tmp_path / "data", tmp_path / "missing.json")
