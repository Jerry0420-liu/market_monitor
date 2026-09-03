from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from market_monitor_data.tdx.models import (
    TdxBlockDownload,
    TdxInstrument,
    TdxRawBar,
    TdxRawQuote,
    TdxSecurity,
)
from market_monitor_data.tdx.protocol import TdxProtocol


class TdxRequestTransport(Protocol):
    def request(
        self,
        payload: bytes,
        *,
        probe: bool = False,
        probe_name: str | None = None,
        validator: Callable[[bytes], None] | None = None,
        setup_payloads: tuple[bytes, ...] = (),
    ) -> bytes: ...


class TdxLiveClient:
    def __init__(self, transport: TdxRequestTransport, protocol: TdxProtocol) -> None:
        self._transport = transport
        self._protocol = protocol

    def security_directory(self) -> tuple[TdxSecurity, ...]:
        securities: list[TdxSecurity] = []
        for market in (0, 1):
            count = self._protocol.parse_security_count(
                self._request(self._protocol.security_count_request(market))
            )
            for start in range(0, count, 1000):
                for record in self._protocol.parse_security_list(
                    self._request(self._protocol.security_list_request(market, start))
                ):
                    securities.append(
                        TdxSecurity(
                            market,
                            record.code,
                            record.name or record.code,
                            _kind(market, record.code),
                            "DISCOVERED",
                        )
                    )
        return tuple(securities)

    def quotes(
        self, instruments: tuple[TdxInstrument, ...], *, probe_name: str | None = None
    ) -> tuple[TdxRawQuote, ...]:
        return self._protocol.parse_quote_response(
            self._request(self._protocol.quote_request(instruments), probe_name=probe_name)
        )

    def block_file(self, filename: str) -> TdxBlockDownload:
        meta = self._protocol.parse_block_meta_response(
            self._request(self._protocol.block_meta_request(filename))
        )
        chunks: list[bytes] = []
        for offset in range(0, meta.size_bytes, 0x7530):
            expected = min(0x7530, meta.size_bytes - offset)
            chunk = self._protocol.parse_block_chunk_response(
                self._request(self._protocol.block_chunk_request(filename, offset, expected))
            )
            if len(chunk) != expected:
                raise ValueError("TDX block chunk length mismatch")
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != meta.size_bytes:
            raise ValueError("TDX block file length mismatch")
        return TdxBlockDownload(filename, meta.server_hash, data, _last_server(self._transport))

    def bars(
        self,
        instrument: TdxInstrument,
        interval: str,
        *,
        start: int = 0,
        count: int = 800,
        probe_name: str | None = None,
    ) -> tuple[TdxRawBar, ...]:
        categories = {"1m": 8, "1d": 9}
        try:
            category = categories[interval]
        except KeyError as error:
            raise ValueError("TDX interval must be 1m or 1d") from error
        return self._protocol.parse_bar_response(
            self._request(
                self._protocol.bar_request(category, instrument, start, count),
                probe_name=probe_name,
            ),
            category,
            instrument,
        )

    def representative_probe(self, observed_at: datetime) -> dict[str, bool]:
        if observed_at.tzinfo is None:
            raise ValueError("TDX probe time must be timezone-aware")
        quote_probes = (
            ("SH_STOCK", TdxInstrument(1, "600000", "STOCK")),
            ("SZ_STOCK", TdxInstrument(0, "000001", "STOCK")),
            ("INDEX", TdxInstrument(1, "000001", "INDEX")),
            ("ETF", TdxInstrument(1, "510300", "ETF")),
        )
        results: dict[str, bool] = {}
        for name, instrument in quote_probes:
            results[name] = self._probe_quote(name, instrument)
        results["MINUTE_BAR"] = self._probe_minute_bar(
            "MINUTE_BAR", TdxInstrument(1, "600000", "STOCK"), observed_at
        )
        return results

    def _probe_quote(self, probe_name: str, instrument: TdxInstrument) -> bool:
        def validate(body: bytes) -> None:
            records = self._protocol.parse_quote_response(body)
            if len(records) != 1 or (records[0].market, records[0].code) != (
                instrument.market,
                instrument.code,
            ):
                raise ValueError("TDX quote probe identity mismatch")

        try:
            self._request(
                self._protocol.quote_request((instrument,)),
                probe_name=probe_name,
                validator=validate,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _probe_minute_bar(
        self, probe_name: str, instrument: TdxInstrument, observed_at: datetime
    ) -> bool:
        def validate(body: bytes) -> None:
            bars = self._protocol.parse_bar_response(body, 8, instrument)
            if not bars:
                raise ValueError("TDX minute-bar probe was empty")
            newest = max(bar.timestamp.astimezone(UTC) for bar in bars)
            if observed_at.astimezone(UTC) - newest > timedelta(minutes=10):
                raise ValueError("TDX minute-bar probe was stale")

        try:
            self._request(
                self._protocol.bar_request(8, instrument, 0, 1),
                probe_name=probe_name,
                validator=validate,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _request(
        self,
        payload: bytes,
        *,
        probe_name: str | None = None,
        validator: Callable[[bytes], None] | None = None,
    ) -> bytes:
        return self._transport.request(
            payload,
            probe=probe_name is not None,
            probe_name=probe_name,
            validator=validator,
            setup_payloads=self._protocol.setup_requests(),
        )


def _kind(market: int, code: str) -> str:
    if (market == 1 and code == "000001") or (market == 0 and code in {"399001", "399006"}):
        return "INDEX"
    if (market == 1 and code[:2] in {"51", "56"}) or (market == 0 and code[:2] in {"15", "16"}):
        return "ETF"
    return "STOCK"


def _last_server(transport: object) -> str | None:
    source = getattr(transport, "last_server", None)
    value = source() if callable(source) else None
    return value if isinstance(value, str) and value else None
