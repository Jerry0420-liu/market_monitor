from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

_REFERENCE_FILE = (
    Path(__file__).parents[1] / "deploy" / "references" / "listing-reference-2026-09-08.json"
)
_PRIMARY_PREFIXES = {
    "SSE": {"600", "601", "603", "605", "688"},
    "SZSE": {"000", "001", "002", "003", "300", "301"},
}


def test_listing_reference_snapshot_is_complete_for_frozen_primary_rules() -> None:
    document = json.loads(_REFERENCE_FILE.read_text(encoding="utf-8"))
    facts = document["facts"]
    by_exchange = {
        exchange: [fact for fact in facts if fact["exchange"] == exchange]
        for exchange in ("SSE", "SZSE")
    }

    assert len(facts) == 5_228
    assert {exchange: len(items) for exchange, items in by_exchange.items()} == {
        "SSE": 2_329,
        "SZSE": 2_899,
    }
    assert all(fact["listing_status"] == "LISTED" for fact in facts)
    assert all(
        fact["trading_code"][:3] in _PRIMARY_PREFIXES[fact["exchange"]]
        or fact["trading_code"][:3] in {"689", "302"}
        for fact in facts
    )
    assert (
        sum(fact["trading_code"][:3] in _PRIMARY_PREFIXES[fact["exchange"]] for fact in facts)
        == 5_226
    )


def test_listing_reference_provenance_records_pagination_and_exclusions() -> None:
    document = json.loads(_REFERENCE_FILE.read_text(encoding="utf-8"))
    sources = {source["exchange"]: source for source in document["provenance"]["sources"]}

    assert sources["SSE"]["stock_types"] == ["10"]
    assert sources["SSE"]["pagination"] == {
        "page_size": 100,
        "pages": 24,
        "rows": 2_332,
        "valid_a_rows": 2_329,
        "non_a_rows": 3,
        "non_a_reason": "B-only rows have no SECURITY_CODE_A",
    }
    assert sources["SZSE"]["artifact_rows"] == 2_899
    assert sources["SZSE"]["primary_candidate_rows"] == 2_898
    assert sources["SZSE"]["frozen_exclusion_prefixes"] == {"302": 1}


def test_listing_reference_prefix_counts_are_reconciled() -> None:
    document = json.loads(_REFERENCE_FILE.read_text(encoding="utf-8"))
    counts = {
        exchange: dict(
            sorted(
                Counter(
                    fact["trading_code"][:3]
                    for fact in document["facts"]
                    if fact["exchange"] == exchange
                ).items()
            )
        )
        for exchange in ("SSE", "SZSE")
    }

    assert counts == {
        "SSE": {"600": 755, "601": 226, "603": 618, "605": 113, "688": 616, "689": 1},
        "SZSE": {"000": 412, "001": 121, "002": 919, "003": 42, "300": 936, "301": 468, "302": 1},
    }
