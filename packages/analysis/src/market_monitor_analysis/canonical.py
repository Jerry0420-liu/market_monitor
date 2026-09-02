import json
from typing import Any

from market_monitor_persistence.values import sha256_bytes


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))
