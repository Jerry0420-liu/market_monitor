import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from market_monitor_data.models import ProviderBatch, ProviderRecord


class ProviderFormatError(ValueError):
    pass


class ProviderDisabledError(RuntimeError):
    pass


class MarketDataProvider(Protocol):
    def batches(self) -> Iterable[ProviderBatch]: ...


class FixtureReplayProvider:
    def __init__(self, path: Path) -> None:
        self._path = path

    def batches(self) -> Iterable[ProviderBatch]:
        document = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("batches"), list):
            raise ProviderFormatError("fixture must contain a batches array")
        for raw_batch in document["batches"]:
            yield _parse_batch(raw_batch)


class DisabledLiveProvider:
    def batches(self) -> Iterable[ProviderBatch]:
        raise ProviderDisabledError("live market provider is disabled")


def _parse_batch(value: Any) -> ProviderBatch:
    if not isinstance(value, dict) or not isinstance(value.get("records"), list):
        raise ProviderFormatError("batch must contain records")
    try:
        batch_id = value["id"]
        received_at = value["received_at"]
        records = tuple(_parse_record(record) for record in value["records"])
    except KeyError as error:
        raise ProviderFormatError(f"missing field: {error.args[0]}") from error
    if not isinstance(batch_id, str) or not isinstance(received_at, str):
        raise ProviderFormatError("batch id and received_at must be strings")
    return ProviderBatch(batch_id, received_at, records)


def _parse_record(value: Any) -> ProviderRecord:
    if not isinstance(value, dict):
        raise ProviderFormatError("record must be an object")
    code = value.get("code")
    source_time = value.get("source_time")
    price = value.get("price")
    volume = value.get("volume")
    if not isinstance(code, str):
        raise ProviderFormatError("record code must be a string")
    if source_time is not None and not isinstance(source_time, str):
        raise ProviderFormatError("source_time must be a string or null")
    if price is not None and not isinstance(price, str):
        raise ProviderFormatError("price must be a decimal string or null")
    if volume is not None and (not isinstance(volume, int) or isinstance(volume, bool)):
        raise ProviderFormatError("volume must be an integer or null")
    return ProviderRecord(code, source_time, price, volume, dict(value))
