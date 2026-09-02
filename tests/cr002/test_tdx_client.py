from __future__ import annotations

import struct
from collections.abc import Callable
from datetime import UTC, datetime

from market_monitor_data.tdx.client import TdxLiveClient
from market_monitor_data.tdx.models import TdxInstrument
from market_monitor_data.tdx.protocol import TdxProtocol


def _directory_body(*records: tuple[str, str]) -> bytes:
    body = bytearray(struct.pack("<H", len(records)))
    for code, name in records:
        body.extend(
            struct.pack(
                "<6sH8s4sBI4s",
                code.encode("ascii"),
                100,
                name.encode("gbk").ljust(8, b"\x00"),
                b"\x00" * 4,
                2,
                0,
                b"\x00" * 4,
            )
        )
    return bytes(body)


class _Pool:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses
        self.calls: list[tuple[bytes, tuple[bytes, ...]]] = []
        self.probe_names: list[str | None] = []

    def request(
        self,
        payload: bytes,
        *,
        probe: bool = False,
        probe_name: str | None = None,
        validator: Callable[[bytes], None] | None = None,
        setup_payloads: tuple[bytes, ...] = (),
    ) -> bytes:
        del probe
        self.calls.append((payload, setup_payloads))
        self.probe_names.append(probe_name)
        response = self._responses.pop(0)
        if validator is not None:
            validator(response)
        return response

    def last_server(self) -> str:
        return "fixture:7709"


def test_live_client_reads_both_directories_with_native_setup_packets() -> None:
    pool = _Pool(
        [
            struct.pack("<H", 1),
            _directory_body(("000001", "平安银行")),
            struct.pack("<H", 2),
            _directory_body(("000001", "上证指数"), ("600000", "浦发银行")),
        ]
    )
    protocol = TdxProtocol()
    client = TdxLiveClient(pool, protocol)

    securities = client.security_directory()

    assert [(item.market, item.code, item.kind) for item in securities] == [
        (0, "000001", "STOCK"),
        (1, "000001", "INDEX"),
        (1, "600000", "STOCK"),
    ]
    assert len(pool.calls) == 4
    assert all(setup == protocol.setup_requests() for _, setup in pool.calls)


def test_live_client_downloads_verified_size_block_chunks() -> None:
    block = b"block-content"
    pool = _Pool(
        [
            struct.pack("<I1s32s1s", len(block), b"\x00", b"hash".ljust(32, b"\x00"), b"\x00"),
            b"\x00\x00\x00\x00" + block,
        ]
    )
    client = TdxLiveClient(pool, TdxProtocol())

    download = client.block_file("block_gn.dat")

    assert download.data == block
    assert download.server_hash == "hash"
    assert download.source_server == "fixture:7709"
    assert len(pool.calls) == 2


def test_live_client_requests_one_minute_and_daily_bars() -> None:
    pool = _Pool([b"\x00\x00", b"\x00\x00"])
    protocol = TdxProtocol()
    client = TdxLiveClient(pool, protocol)
    instrument = TdxInstrument(1, "600000", "STOCK")

    assert client.bars(instrument, "1m", count=1) == ()
    assert client.bars(instrument, "1d", count=1) == ()
    assert pool.calls[0][0] == protocol.bar_request(8, instrument, 0, 1)
    assert pool.calls[1][0] == protocol.bar_request(9, instrument, 0, 1)


def test_live_client_runs_identity_checked_representative_probes() -> None:
    pool = _Pool(
        [
            _quote_body(1, "600000"),
            _quote_body(0, "000001"),
            _quote_body(1, "000001"),
            _quote_body(1, "510300"),
            _minute_bar_body(),
        ]
    )
    client = TdxLiveClient(pool, TdxProtocol())

    result = client.representative_probe(datetime(2026, 8, 21, 1, 30, tzinfo=UTC))

    assert result == {
        "SH_STOCK": True,
        "SZ_STOCK": True,
        "INDEX": True,
        "ETF": True,
        "MINUTE_BAR": True,
    }
    assert pool.probe_names == ["SH_STOCK", "SZ_STOCK", "INDEX", "ETF", "MINUTE_BAR"]


def _number(value: int) -> bytes:
    magnitude = abs(value)
    first = magnitude & 0x3F
    magnitude >>= 6
    output = bytearray([first | (0x40 if value < 0 else 0)])
    if magnitude:
        output[0] |= 0x80
    while magnitude:
        byte = magnitude & 0x7F
        magnitude >>= 7
        if magnitude:
            byte |= 0x80
        output.append(byte)
    return bytes(output)


def _quote_body(market: int, code: str) -> bytes:
    fields = (1025, -25, 5, 10, -15, 93000, -1025, 1000, 20)
    record = bytearray(struct.pack("<B6sH", market, code.encode("ascii"), 0))
    record.extend(b"".join(_number(value) for value in fields))
    record.extend(struct.pack("<I", 0))
    record.extend(b"".join(_number(0) for _ in range(24)))
    record.extend(struct.pack("<H", 0))
    record.extend(b"".join(_number(0) for _ in range(4)))
    record.extend(struct.pack("<hH", 0, 0))
    return b"\xb1\xcb" + struct.pack("<H", 1) + bytes(record)


def _minute_bar_body() -> bytes:
    packed_day = ((2026 - 2004) << 11) + 8 * 100 + 21
    return (
        struct.pack("<H", 1)
        + struct.pack("<HH", packed_day, 9 * 60 + 30)
        + b"".join(_number(value) for value in (10_250, 250, 500, -250))
        + struct.pack("<f", 500.0)
        + struct.pack("<f", 5000.0)
    )
