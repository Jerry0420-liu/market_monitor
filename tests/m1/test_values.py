from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from market_monitor_persistence.values import (
    format_rfc3339,
    from_scaled_integer,
    new_uid,
    parse_rfc3339,
    sha256_bytes,
    sha256_file,
    to_scaled_integer,
    utc_now,
)


def test_new_uid_is_unique_opaque_uuid7() -> None:
    first = new_uid()
    second = new_uid()

    assert first != second
    assert UUID(first).version == 7
    assert UUID(second).version == 7


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is UTC


def test_rfc3339_round_trip_normalizes_to_utc() -> None:
    instant = datetime(2026, 8, 4, 3, 2, 1, 123456, tzinfo=UTC)

    encoded = format_rfc3339(instant)

    assert encoded == "2026-08-04T03:02:01.123456Z"
    assert parse_rfc3339(encoded) == instant


def test_rfc3339_rejects_naive_or_timezone_free_values() -> None:
    with pytest.raises(ValueError, match="timezone"):
        format_rfc3339(datetime(2026, 8, 4, 3, 2, 1))
    with pytest.raises(ValueError, match="timezone"):
        parse_rfc3339("2026-08-04T03:02:01")


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        (Decimal("1.2345"), 4, 12345),
        ("0", 2, 0),
        ("-12.30", 2, -1230),
    ],
)
def test_fixed_point_uses_scaled_integers(value: Decimal | str, scale: int, expected: int) -> None:
    assert to_scaled_integer(value, scale) == expected
    assert from_scaled_integer(expected, scale) == Decimal(str(value))


def test_fixed_point_rejects_float_and_precision_loss() -> None:
    with pytest.raises(TypeError, match="float"):
        to_scaled_integer(1.25, 2)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="precision"):
        to_scaled_integer("1.234", 2)
    with pytest.raises(ValueError, match="non-negative"):
        to_scaled_integer("1", -1)


def test_sha256_helpers_match_known_vector(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"abc")
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    assert sha256_bytes(b"abc") == expected
    assert sha256_file(path) == expected
