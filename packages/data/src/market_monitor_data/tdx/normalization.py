from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from market_monitor_data.tdx.models import TdxBar, TdxInstrument, TdxQuote, TdxRawBar, TdxRawQuote


@dataclass(frozen=True)
class TdxInvalidRecord:
    reason: str
    detail: str


def normalize_quote(
    raw: TdxRawQuote,
    instrument: TdxInstrument,
    received_at: datetime,
    *,
    previous: TdxQuote | None = None,
) -> TdxQuote | TdxInvalidRecord:
    if raw.market != instrument.market or raw.code != instrument.code:
        return TdxInvalidRecord("INVALID_RESPONSE", "returned market/code does not match request")
    if not _finite(raw.price, raw.last_close, raw.open, raw.high, raw.low, raw.amount):
        return TdxInvalidRecord("INVALID_RESPONSE", "quote contains a non-finite numeric value")
    if raw.volume_lots < 0 or raw.current_volume_lots < 0 or raw.amount < 0:
        return TdxInvalidRecord("INVALID_RESPONSE", "quote contains a negative cumulative value")
    if _is_no_valid_quote(raw):
        return TdxQuote(
            instrument=instrument,
            price=None,
            pre_close=None,
            open=None,
            high=None,
            low=None,
            volume=None,
            volume_unit=_quote_volume_unit(instrument.kind),
            amount=None,
            quote_status="NO_VALID_QUOTE",
            source_time=None,
            server_time_raw=raw.server_time_raw,
            received_at=received_at,
        )
    volume, unit = _quote_volume(raw.volume_lots, instrument.kind)
    result = TdxQuote(
        instrument=instrument,
        price=raw.price,
        pre_close=raw.last_close,
        open=raw.open,
        high=raw.high,
        low=raw.low,
        volume=volume,
        volume_unit=unit,
        amount=raw.amount,
        quote_status="VALUE",
        source_time=None,
        server_time_raw=raw.server_time_raw,
        received_at=received_at,
    )
    if previous is not None and previous.quote_status == "VALUE":
        if (
            previous.volume is not None
            and result.volume is not None
            and result.volume < previous.volume
        ) or (
            previous.amount is not None
            and result.amount is not None
            and result.amount < previous.amount
        ):
            return TdxInvalidRecord(
                "CUMULATIVE_DECREASE", "quote cumulative amount or volume decreased"
            )
    return result


def normalize_bar(
    raw: TdxRawBar,
    instrument: TdxInstrument,
    interval: Literal["1m", "1d"],
) -> TdxBar | TdxInvalidRecord:
    if raw.market != instrument.market or raw.code != instrument.code:
        return TdxInvalidRecord("INVALID_RESPONSE", "returned market/code does not match request")
    if (
        raw.volume < 0
        or raw.amount < 0
        or not _finite(raw.open, raw.high, raw.low, raw.close, raw.amount)
    ):
        return TdxInvalidRecord("INVALID_RESPONSE", "bar contains invalid values")
    if instrument.kind == "INDEX":
        unit = "INDEX_SHARE_LIKE" if interval == "1m" else "INDEX_LOT_LIKE"
    elif instrument.kind in {"STOCK", "ETF"}:
        unit = "SHARES"
    else:
        return TdxInvalidRecord(
            "INVALID_INSTRUMENT", f"unsupported instrument kind: {instrument.kind}"
        )
    return TdxBar(
        instrument=instrument,
        interval=interval,
        timestamp=raw.timestamp,
        open=raw.open,
        high=raw.high,
        low=raw.low,
        close=raw.close,
        volume=raw.volume,
        volume_unit=unit,
        amount=raw.amount,
    )


def _quote_volume(volume_lots: int, kind: str) -> tuple[int, str]:
    if kind in {"STOCK", "ETF"}:
        return volume_lots * 100, "SHARES"
    if kind == "INDEX":
        return volume_lots, "INDEX_LOT_LIKE"
    raise ValueError(f"unsupported instrument kind: {kind}")


def _quote_volume_unit(kind: str) -> str:
    return _quote_volume(0, kind)[1]


def _is_no_valid_quote(raw: TdxRawQuote) -> bool:
    return (
        raw.price == raw.open == raw.high == raw.low == 0
        and raw.last_close > 0
        and raw.amount < Decimal("1e-30")
    )


def _finite(*values: Decimal) -> bool:
    return all(value.is_finite() for value in values)
