from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid7


def new_uid() -> str:
    return str(uuid7())


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("RFC3339 value must include a timezone")
    return parsed.astimezone(UTC)


def to_scaled_integer(value: Decimal | str, scale: int) -> int:
    if isinstance(value, float):
        raise TypeError("float values are not accepted")
    if scale < 0:
        raise ValueError("scale must be non-negative")
    decimal_value = value if isinstance(value, Decimal) else Decimal(value)
    if not decimal_value.is_finite():
        raise ValueError("value must be finite")
    scaled = decimal_value.scaleb(scale)
    if scaled != scaled.to_integral_value():
        raise ValueError("value exceeds fixed-point precision")
    return int(scaled)


def from_scaled_integer(value: int, scale: int) -> Decimal:
    if scale < 0:
        raise ValueError("scale must be non-negative")
    return Decimal(value).scaleb(-scale)


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
