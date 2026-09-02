from __future__ import annotations

import struct
import zlib
from decimal import Decimal

import pytest
from market_monitor_data.tdx.models import TdxInstrument
from market_monitor_data.tdx.protocol import TdxProtocol, TdxProtocolError


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


def _quote_record(*, price: int = 1025, close_difference: int = -25) -> bytes:
    fields = [
        price,
        close_difference,
        5,
        10,
        -15,
        93000,  # TDX time-of-day field
        -1025,
        1000,  # lots
        20,
    ]
    payload = bytearray(struct.pack("<B6sH", 1, b"600000", 0))
    payload.extend(b"".join(_number(value) for value in fields))
    payload.extend(struct.pack("<I", 0))
    payload.extend(b"".join(_number(0) for _ in range(24)))
    payload.extend(struct.pack("<H", 0))
    payload.extend(b"".join(_number(0) for _ in range(4)))
    payload.extend(struct.pack("<hH", 0, 0))
    return bytes(payload)


def test_quote_request_has_native_wire_shape() -> None:
    packet = TdxProtocol().quote_request((TdxInstrument(0, "000001", "STOCK"),))

    assert packet.hex() == "0c0120630002130013003e050500000000000000010000303030303031"


def test_quote_request_rejects_empty_or_oversized_batches() -> None:
    protocol = TdxProtocol()
    instrument = TdxInstrument(1, "600000", "STOCK")

    with pytest.raises(TdxProtocolError, match="empty"):
        protocol.quote_request(())
    with pytest.raises(TdxProtocolError, match="75"):
        protocol.quote_request((instrument,) * 76)


def test_security_directory_packets_and_records_have_known_shapes() -> None:
    protocol = TdxProtocol()
    body = struct.pack("<H", 1) + struct.pack(
        "<6sH8s4sBI4s",
        b"600000",
        100,
        "浦发银行".encode("gbk").ljust(8, b"\x00"),
        b"\x00" * 4,
        2,
        0,
        b"\x00" * 4,
    )

    assert protocol.security_count_request(1).hex() == "0c0c186c0001080008004e04010075c73301"
    assert protocol.security_list_request(1, 0).hex() == "0c011864010106000600500401000000"
    assert protocol.parse_security_count(struct.pack("<H", 2)) == 2
    assert protocol.parse_security_list(body)[0].code == "600000"
    assert protocol.parse_security_list(body)[0].name == "浦发银行"


def test_response_frame_decompresses_and_rejects_truncation() -> None:
    protocol = TdxProtocol()
    body = b"known TDX fixture"
    compressed = zlib.compress(body)
    header = struct.pack("<IIIHH", 0, 0, 0, len(compressed), len(body))

    assert protocol.decode_response(header, compressed) == body

    with pytest.raises(TdxProtocolError, match="header"):
        protocol.decode_response(header[:8], compressed)
    with pytest.raises(TdxProtocolError, match="truncated"):
        protocol.decode_response(header, compressed[:-1])


def test_quote_response_decodes_native_values() -> None:
    protocol = TdxProtocol()
    body = b"\xb1\xcb" + struct.pack("<H", 1) + _quote_record()

    records = protocol.parse_quote_response(body)

    assert len(records) == 1
    assert records[0].market == 1
    assert records[0].code == "600000"
    assert records[0].price == Decimal("10.25")
    assert records[0].last_close == Decimal("10.00")
    assert records[0].open == Decimal("10.30")
    assert records[0].high == Decimal("10.35")
    assert records[0].low == Decimal("10.10")
    assert records[0].volume_lots == 1000
    assert records[0].server_time_raw == 93000


def test_quote_response_rejects_truncated_variable_number() -> None:
    protocol = TdxProtocol()
    body = b"\xb1\xcb" + struct.pack("<H", 1) + struct.pack("<B6sH", 1, b"600000", 0) + b"\x80"

    with pytest.raises(TdxProtocolError, match="truncated"):
        protocol.parse_quote_response(body)


def test_bar_request_and_response_keep_native_price_deltas() -> None:
    protocol = TdxProtocol()
    instrument = TdxInstrument(1, "600000", "STOCK")
    zip_day = ((2026 - 2004) << 11) + 8 * 100 + 21
    body = (
        struct.pack("<H", 1)
        + struct.pack("<HH", zip_day, 9 * 60 + 30)
        + b"".join(_number(value) for value in (10_250, 250, 500, -250))
        + struct.pack("<f", 500.0)
        + struct.pack("<f", 5000.0)
    )

    bars = protocol.parse_bar_response(body, 8, instrument)

    assert len(protocol.bar_request(8, instrument, 0, 1)) == 38
    assert bars[0].open == Decimal("10.25")
    assert bars[0].close == Decimal("10.5")
    assert bars[0].high == Decimal("10.75")
    assert bars[0].low == Decimal("10")
    assert bars[0].volume == 500
    assert bars[0].amount == Decimal("5000")


def test_index_bars_consume_the_index_only_advance_decline_fields() -> None:
    protocol = TdxProtocol()
    instrument = TdxInstrument(1, "000001", "INDEX")
    packed_day = ((2026 - 2004) << 11) + 8 * 100 + 21
    body = (
        struct.pack("<H", 1)
        + struct.pack("<HH", packed_day, 9 * 60 + 30)
        + b"".join(_number(value) for value in (10_250, 250, 500, -250))
        + struct.pack("<f", 500.0)
        + struct.pack("<f", 5000.0)
        + struct.pack("<HH", 123, 456)
    )

    bars = protocol.parse_bar_response(body, 8, instrument)

    assert len(bars) == 1
    assert bars[0].close == Decimal("10.5")


def test_block_meta_and_chunk_packets_preserve_source_hash() -> None:
    protocol = TdxProtocol()
    body = struct.pack("<I1s32s1s", 123, b"\x00", b"server-hash".ljust(32, b"\x00"), b"\x00")

    meta = protocol.parse_block_meta_response(body)

    assert len(protocol.block_meta_request("block_gn.dat")) == 52
    assert len(protocol.block_chunk_request("block_gn.dat", 0, 123)) == 120
    assert meta.size_bytes == 123
    assert meta.server_hash == "server-hash"
    assert protocol.parse_block_chunk_response(b"\x00\x00\x00\x00payload") == b"payload"


def test_quote_response_preserves_negative_multibyte_differences() -> None:
    protocol = TdxProtocol()
    body = (
        b"\xb1\xcb"
        + struct.pack("<H", 1)
        + _quote_record(price=2_000_000, close_difference=-20_000)
    )

    record = protocol.parse_quote_response(body)[0]

    assert record.price == Decimal("20000")
    assert record.last_close == Decimal("19800")
