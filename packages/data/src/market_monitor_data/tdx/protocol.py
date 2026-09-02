from __future__ import annotations

import struct
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from market_monitor_data.tdx.models import (
    TdxBlockMeta,
    TdxDirectoryRecord,
    TdxInstrument,
    TdxRawBar,
    TdxRawQuote,
)


class TdxProtocolError(ValueError):
    """A TDX packet is malformed, incomplete, or unsafe to accept."""


class TdxProtocol:
    MAX_QUOTE_BATCH = 75
    RESPONSE_HEADER_SIZE = 16
    MAX_RESPONSE_BYTES = 32 * 1024 * 1024

    def setup_requests(self) -> tuple[bytes, bytes, bytes]:
        return (
            bytes.fromhex("0c0218930001030003000d0001"),
            bytes.fromhex("0c0218940001030003000d0002"),
            bytes.fromhex(
                "0c031899000120002000db0fd5d0c9ccd6a4a8af0000008fc22540130000d500c9cc"
                "bdf0d7ea00000002"
            ),
        )

    def security_count_request(self, market: int) -> bytes:
        _market(market)
        return (
            bytes.fromhex("0c0c186c0001080008004e04")
            + struct.pack("<H", market)
            + bytes.fromhex("75c73301")
        )

    def security_list_request(self, market: int, start: int) -> bytes:
        _market(market)
        if not 0 <= start <= 0xFFFF:
            raise TdxProtocolError("security directory start is outside uint16 range")
        return bytes.fromhex("0c0118640101060006005004") + struct.pack("<HH", market, start)

    def bar_request(
        self, category: int, instrument: TdxInstrument, start: int, count: int
    ) -> bytes:
        if not 0 <= category <= 11:
            raise TdxProtocolError("unsupported TDX bar category")
        if start < 0 or not 1 <= count <= 800:
            raise TdxProtocolError("invalid TDX bar range")
        return struct.pack(
            "<HIHHHH6sHHHHIIH",
            0x10C,
            0x01016408,
            0x1C,
            0x1C,
            0x052D,
            instrument.market,
            instrument.code.encode("ascii"),
            category,
            1,
            start,
            count,
            0,
            0,
            0,
        )

    def block_meta_request(self, filename: str) -> bytes:
        _block_filename(filename)
        return bytes.fromhex("0c39186900012a002a00c502") + struct.pack(
            "<40s", filename.encode("ascii")
        )

    def block_chunk_request(self, filename: str, offset: int, size: int) -> bytes:
        _block_filename(filename)
        if offset < 0 or not 1 <= size <= 0x7530:
            raise TdxProtocolError("invalid TDX block chunk range")
        return bytes.fromhex("0c37186a00016e006e00b906") + struct.pack(
            "<II100s", offset, size, filename.encode("ascii")
        )

    def quote_request(self, instruments: tuple[TdxInstrument, ...]) -> bytes:
        if not instruments:
            raise TdxProtocolError("empty quote request")
        if len(instruments) > self.MAX_QUOTE_BATCH:
            raise TdxProtocolError("quote request exceeds 75 symbols")
        data_length = len(instruments) * 7 + 12
        packet = bytearray(
            struct.pack(
                "<HIHHIIHH",
                0x10C,
                0x02006320,
                data_length,
                data_length,
                0x5053E,
                0,
                0,
                len(instruments),
            )
        )
        for instrument in instruments:
            packet.extend(struct.pack("<B6s", instrument.market, instrument.code.encode("ascii")))
        return bytes(packet)

    def decode_response(self, header: bytes, body: bytes) -> bytes:
        if len(header) != self.RESPONSE_HEADER_SIZE:
            raise TdxProtocolError("invalid response header")
        _, _, _, compressed_length, uncompressed_length = struct.unpack("<IIIHH", header)
        if (
            compressed_length > self.MAX_RESPONSE_BYTES
            or uncompressed_length > self.MAX_RESPONSE_BYTES
        ):
            raise TdxProtocolError("response exceeds safe size")
        if len(body) != compressed_length:
            raise TdxProtocolError("truncated response body")
        try:
            decoded = body if compressed_length == uncompressed_length else zlib.decompress(body)
        except zlib.error as error:
            raise TdxProtocolError("invalid compressed response") from error
        if len(decoded) != uncompressed_length:
            raise TdxProtocolError("response length mismatch")
        return decoded

    def parse_security_count(self, body: bytes) -> int:
        if len(body) != 2:
            raise TdxProtocolError("invalid security count response")
        return _read_u16(body, 0)

    def parse_security_list(self, body: bytes) -> tuple[TdxDirectoryRecord, ...]:
        count = _read_u16(body, 0)
        expected = 2 + count * 29
        if len(body) != expected:
            raise TdxProtocolError("truncated security directory response")
        records: list[TdxDirectoryRecord] = []
        position = 2
        for _ in range(count):
            code_bytes, volume_unit, name_bytes, _, decimal_point, _, _ = struct.unpack(
                "<6sH8s4sBI4s", body[position : position + 29]
            )
            position += 29
            try:
                code = code_bytes.decode("ascii")
            except UnicodeDecodeError as error:
                raise TdxProtocolError("invalid directory code") from error
            if len(code) != 6 or not code.isdigit():
                raise TdxProtocolError("invalid directory code")
            records.append(
                TdxDirectoryRecord(
                    code,
                    name_bytes.decode("gbk", errors="replace").rstrip("\x00"),
                    volume_unit,
                    decimal_point,
                )
            )
        return tuple(records)

    def parse_quote_response(self, body: bytes) -> tuple[TdxRawQuote, ...]:
        if len(body) < 4:
            raise TdxProtocolError("truncated quote response")
        position = 2
        count = _read_u16(body, position)
        position += 2
        records: list[TdxRawQuote] = []
        for _ in range(count):
            record, position = _parse_quote(body, position)
            records.append(record)
        if position != len(body):
            raise TdxProtocolError("unexpected quote response trailing bytes")
        return tuple(records)

    def parse_bar_response(
        self, body: bytes, category: int, instrument: TdxInstrument
    ) -> tuple[TdxRawBar, ...]:
        count = _read_u16(body, 0)
        position = 2
        base = 0
        bars: list[TdxRawBar] = []
        for _ in range(count):
            timestamp, position = _read_bar_time(body, position, category)
            open_difference, position = _read_number(body, position)
            close_difference, position = _read_number(body, position)
            high_difference, position = _read_number(body, position)
            low_difference, position = _read_number(body, position)
            _require(body, position, 8)
            volume = _read_float_decimal(body[position : position + 4])
            amount = _read_float_decimal(body[position + 4 : position + 8])
            position += 8
            if instrument.kind == "INDEX":
                # ponytail: consume index breadth fields.
                # Model them only if an approved analysis needs them.
                _require(body, position, 4)
                position += 4
            if volume < 0 or volume != volume.to_integral_value():
                raise TdxProtocolError("invalid TDX bar volume")
            open_raw = base + open_difference
            bars.append(
                TdxRawBar(
                    instrument.market,
                    instrument.code,
                    timestamp,
                    _bar_price(open_raw),
                    _bar_price(open_raw + high_difference),
                    _bar_price(open_raw + low_difference),
                    _bar_price(open_raw + close_difference),
                    int(volume),
                    amount,
                )
            )
            base = open_raw + close_difference
        if position != len(body):
            raise TdxProtocolError("unexpected bar response trailing bytes")
        return tuple(bars)

    def parse_block_meta_response(self, body: bytes) -> TdxBlockMeta:
        if len(body) != 38:
            raise TdxProtocolError("invalid TDX block metadata response")
        size_bytes, _, raw_hash, _ = struct.unpack("<I1s32s1s", body)
        value = raw_hash.rstrip(b"\x00")
        if not value:
            server_hash = None
        else:
            try:
                server_hash = value.decode("ascii")
            except UnicodeDecodeError:
                server_hash = value.hex()
        return TdxBlockMeta(size_bytes, server_hash)

    def parse_block_chunk_response(self, body: bytes) -> bytes:
        if len(body) < 4:
            raise TdxProtocolError("truncated TDX block chunk response")
        return body[4:]


def _parse_quote(body: bytes, position: int) -> tuple[TdxRawQuote, int]:
    _require(body, position, 9)
    market, code_bytes, _ = struct.unpack("<B6sH", body[position : position + 9])
    position += 9
    if market not in {0, 1}:
        raise TdxProtocolError("invalid quote market")
    try:
        code = code_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise TdxProtocolError("invalid quote code") from error
    if len(code) != 6 or not code.isdigit():
        raise TdxProtocolError("invalid quote code")
    values: list[int] = []
    for _ in range(9):
        value, position = _read_number(body, position)
        values.append(value)
    _require(body, position, 4)
    amount = _read_float_decimal(body[position : position + 4])
    position += 4
    for _ in range(24):
        _, position = _read_number(body, position)
    _require(body, position, 2)
    position += 2
    for _ in range(4):
        _, position = _read_number(body, position)
    _require(body, position, 4)
    position += 4
    price, close_difference, open_difference, high_difference, low_difference = values[:5]
    return (
        TdxRawQuote(
            market=market,
            code=code,
            price=_price(price),
            last_close=_price(price + close_difference),
            open=_price(price + open_difference),
            high=_price(price + high_difference),
            low=_price(price + low_difference),
            volume_lots=values[7],
            current_volume_lots=values[8],
            amount=amount,
            server_time_raw=values[5],
        ),
        position,
    )


def _read_u16(body: bytes, position: int) -> int:
    _require(body, position, 2)
    return int(struct.unpack("<H", body[position : position + 2])[0])


def _read_number(body: bytes, position: int) -> tuple[int, int]:
    _require(body, position, 1)
    first = body[position]
    negative = bool(first & 0x40)
    position += 1
    magnitude = first & 0x3F
    shift = 6
    while first & 0x80:
        _require(body, position, 1)
        first = body[position]
        position += 1
        magnitude |= (first & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise TdxProtocolError("variable number exceeds safe range")
    return (-magnitude if negative else magnitude), position


def _read_float_decimal(data: bytes) -> Decimal:
    bits = struct.unpack("<I", data)[0]
    sign = -1 if bits >> 31 else 1
    exponent = (bits >> 23) & 0xFF
    mantissa = bits & 0x7FFFFF
    if exponent == 0xFF:
        raise TdxProtocolError("non-finite TDX floating value")
    if exponent == 0:
        value = Decimal(mantissa) * (Decimal(2) ** -149)
    else:
        value = (Decimal(1) + Decimal(mantissa) / Decimal(1 << 23)) * (
            Decimal(2) ** (exponent - 127)
        )
    return Decimal(sign) * value


def _price(value: int) -> Decimal:
    return Decimal(value) / Decimal(100)


def _bar_price(value: int) -> Decimal:
    return Decimal(value) / Decimal(1000)


def _read_bar_time(body: bytes, position: int, category: int) -> tuple[datetime, int]:
    _require(body, position, 4)
    if category < 4 or category in {7, 8}:
        packed_date, minutes = struct.unpack("<HH", body[position : position + 4])
        year = (packed_date >> 11) + 2004
        month = (packed_date % 2048) // 100
        day = (packed_date % 2048) % 100
        hour = minutes // 60
        minute = minutes % 60
    else:
        packed_date = struct.unpack("<I", body[position : position + 4])[0]
        year = packed_date // 10000
        month = (packed_date % 10000) // 100
        day = packed_date % 100
        hour = 15
        minute = 0
    try:
        return datetime(
            year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=8))
        ), position + 4
    except ValueError as error:
        raise TdxProtocolError("invalid TDX bar timestamp") from error


def _require(body: bytes, position: int, length: int) -> None:
    if position < 0 or length < 0 or len(body) - position < length:
        raise TdxProtocolError("truncated TDX response")


def _market(value: int) -> None:
    if value not in {0, 1}:
        raise TdxProtocolError("invalid TDX market")


def _block_filename(value: str) -> None:
    if value not in {"block.dat", "block_zs.dat", "block_gn.dat", "block_fg.dat"}:
        raise TdxProtocolError("unsupported TDX block file")
