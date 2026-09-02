from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TdxInstrument:
    market: int
    code: str
    kind: str

    def __post_init__(self) -> None:
        if self.market not in {0, 1}:
            raise ValueError("TDX market must be Shenzhen (0) or Shanghai (1)")
        if len(self.code) != 6 or not self.code.isascii() or not self.code.isdigit():
            raise ValueError("TDX code must contain six digits")


@dataclass(frozen=True)
class TdxSecurity:
    market: int
    code: str
    name: str
    kind: str
    listing_status: str = "DISCOVERED"
    trading_status: str = "TRADING"
    listing_effective_at: datetime | None = None

    def __post_init__(self) -> None:
        TdxInstrument(self.market, self.code, self.kind)
        if not self.name.strip():
            raise ValueError("TDX security name must not be empty")

    @property
    def instrument(self) -> TdxInstrument:
        return TdxInstrument(self.market, self.code, self.kind)


@dataclass(frozen=True)
class TdxDirectoryRecord:
    code: str
    name: str
    volume_unit: int
    decimal_point: int


@dataclass(frozen=True)
class TdxRawQuote:
    market: int
    code: str
    price: Decimal
    last_close: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    volume_lots: int
    current_volume_lots: int
    amount: Decimal
    server_time_raw: int


@dataclass(frozen=True)
class TdxRawBar:
    market: int
    code: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal


@dataclass(frozen=True)
class TdxQuote:
    instrument: TdxInstrument
    price: Decimal | None
    pre_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: int | None
    volume_unit: str
    amount: Decimal | None
    quote_status: str
    source_time: datetime | None
    server_time_raw: int
    received_at: datetime


@dataclass(frozen=True)
class TdxBar:
    instrument: TdxInstrument
    interval: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    volume_unit: str
    amount: Decimal


@dataclass(frozen=True)
class TdxQuoteDetail:
    pre_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    amount: Decimal | None
    quote_status: str
    server_time_raw: int


@dataclass(frozen=True)
class TdxBlockArtifact:
    filename: str
    source_server: str
    size_bytes: int
    server_hash: str | None
    sha256: str
    fetched_at: datetime
    parser_version: str


@dataclass(frozen=True)
class TdxBlockMeta:
    size_bytes: int
    server_hash: str | None


@dataclass(frozen=True)
class TdxBlockDownload:
    filename: str
    server_hash: str | None
    data: bytes
    source_server: str | None = None


@dataclass(frozen=True)
class TdxBlockMembership:
    block_name: str
    block_type: int
    membership_kind: str
    market: int
    code: str
