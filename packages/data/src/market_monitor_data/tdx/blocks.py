from __future__ import annotations

import struct
from collections.abc import Sequence

from market_monitor_data.tdx.models import TdxBlockMembership


class TdxBlockParseError(ValueError):
    """A TDX block artifact is incomplete or cannot be trusted."""


_FILENAMES = frozenset({"block.dat", "block_zs.dat", "block_gn.dat", "block_fg.dat"})
_MEMBER_WIDTH = 7
_MEMBER_REGION_BYTES = 2800
_MAX_BLOCK_MEMBERS = _MEMBER_REGION_BYTES // _MEMBER_WIDTH


def assemble_block_chunks(chunks: Sequence[bytes], expected_size: int) -> bytes:
    if expected_size < 0:
        raise TdxBlockParseError("invalid block length")
    data = b"".join(chunks)
    if len(data) != expected_size:
        raise TdxBlockParseError("block chunk length mismatch")
    return data


def parse_block_file(data: bytes, filename: str) -> tuple[TdxBlockMembership, ...]:
    if filename not in _FILENAMES:
        raise TdxBlockParseError("unsupported TDX block file")
    if len(data) < 386:
        raise TdxBlockParseError("truncated TDX block header")
    position = 384
    count = struct.unpack("<H", data[position : position + 2])[0]
    position += 2
    memberships: list[TdxBlockMembership] = []
    seen: set[tuple[str, int, str]] = set()
    for _ in range(count):
        _require(data, position, 13)
        name = data[position : position + 9].decode("gbk", errors="replace").rstrip("\x00")
        position += 9
        stock_count, block_type = struct.unpack("<HH", data[position : position + 4])
        position += 4
        if stock_count > _MAX_BLOCK_MEMBERS:
            raise TdxBlockParseError("TDX block member count exceeds fixed region")
        member_start = position
        _require(data, member_start, _MEMBER_REGION_BYTES)
        kind = classify_block(filename, name, block_type)
        for index in range(stock_count):
            offset = member_start + index * _MEMBER_WIDTH
            raw_member = data[offset : offset + _MEMBER_WIDTH]
            raw_code = raw_member.decode("ascii", errors="ignore").rstrip("\x00")
            market, code = _block_identity(raw_code, filename)
            key = (name, market, code)
            if key not in seen:
                memberships.append(TdxBlockMembership(name, block_type, kind, market, code))
                seen.add(key)
        position = member_start + _MEMBER_REGION_BYTES
    if position != len(data):
        raise TdxBlockParseError("unexpected TDX block trailing bytes")
    return tuple(memberships)


def classify_block(filename: str, name: str, block_type: int) -> str:
    del block_type
    if filename == "block_zs.dat":
        return "IndexMembership"
    if filename == "block.dat":
        return "TDXCuratedMembership"
    if filename == "block_gn.dat":
        return (
            "ThemeMembership"
            if any(word in name for word in ("主题", "事件", "区域"))
            else "ConceptMembership"
        )
    return (
        "StatusMembership"
        if any(word in name.upper() for word in ("ST", "停牌", "退市", "状态"))
        else "StyleFactorMembership"
    )


def _block_identity(raw_code: str, filename: str) -> tuple[int, str]:
    if len(raw_code) == 7 and raw_code[0] in {"0", "1"}:
        market, code = int(raw_code[0]), raw_code[1:]
    else:
        code = raw_code
        if len(code) != 6 or not code.isdigit():
            raise TdxBlockParseError("invalid TDX block code")
        if code[0] in {"5", "6", "9"}:
            market = 1
        elif filename == "block_zs.dat":
            market = 1
        else:
            market = 0
    if len(code) != 6 or not code.isdigit():
        raise TdxBlockParseError("invalid TDX block code")
    return market, code


def _require(data: bytes, position: int, length: int) -> None:
    if position < 0 or length < 0 or len(data) - position < length:
        raise TdxBlockParseError("truncated TDX block artifact")
