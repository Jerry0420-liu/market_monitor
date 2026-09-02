from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class InstrumentIdentity:
    instrument_uid: str
    version: int
    exchange: str
    trading_code: str
    name: str


@dataclass(frozen=True)
class ReferenceCandidate:
    uid: str
    kind: str
    name: str
    code: str | None


@dataclass(frozen=True)
class ProviderInstrumentRegistration:
    external_code: str
    instrument_kind: str
    exchange: str
    trading_code: str
    name: str
    listing_status: str
    trading_status: str
    listing_effective_at: datetime | None = None
    listing_evidence_sha256: str | None = None
    listing_source_ref: str | None = None


@dataclass(frozen=True)
class ProviderListingState:
    external_code: str
    listing_status: str
    trading_status: str
    listing_effective_at: datetime | None
    listing_evidence_sha256: str | None
    listing_source_ref: str | None


@dataclass(frozen=True)
class ProviderRecord:
    external_code: str
    source_time: str | None
    price: str | None
    volume: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProviderBatch:
    provider_batch_id: str
    received_at: str
    records: tuple[ProviderRecord, ...]


@dataclass(frozen=True)
class IngestionResult:
    batch_uid: str
    quotes: int
    quarantined: int
    lineages: tuple[str, ...]


@dataclass(frozen=True)
class QuoteVersion:
    quote_uid: str
    record_version: int
    source_time_raw: str | None
    source_time: str | None
    price_scaled: int | None
    price_scale: int
    volume: int | None
    is_current: bool


@dataclass(frozen=True)
class HealthStatus:
    health_status: str
    fitness_status: str
    valid_until: str


@dataclass(frozen=True)
class Watermark:
    event_time: str | None
    received_time: str
    version: int
    rewarm_required: bool


@dataclass(frozen=True)
class SectorContextMappingVersion:
    mapping_version_uid: str
    sector_uid: str
    etf_instrument_uid: str | None
    style_instrument_uid: str | None
    owner_approval_ref: str
    source_artifact_sha256: str
    valid_from: str
