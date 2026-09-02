from __future__ import annotations

import struct

import pytest
from market_monitor_data.tdx.blocks import (
    TdxBlockParseError,
    assemble_block_chunks,
    parse_block_file,
)


def _block_file(name: str, block_type: int, codes: tuple[str, ...]) -> bytes:
    header = b"\x00" * 384 + struct.pack("<H", 1)
    return header + _block_record(name, block_type, codes)


def _block_record(name: str, block_type: int, codes: tuple[str, ...]) -> bytes:
    encoded_name = name.encode("gbk")[:9].ljust(9, b"\x00")
    body = encoded_name + struct.pack("<HH", len(codes), block_type)
    members = b"".join(code.encode("ascii").ljust(7, b"\x00") for code in codes)
    return body + members.ljust(2800, b"\x00")


@pytest.mark.parametrize(
    ("filename", "name", "expected_kind"),
    (
        ("block_zs.dat", "上证指数", "IndexMembership"),
        ("block_gn.dat", "人工智能", "ConceptMembership"),
        ("block_fg.dat", "ST板块", "StatusMembership"),
        ("block.dat", "自选集合", "TDXCuratedMembership"),
    ),
)
def test_all_required_block_files_parse_to_non_industry_memberships(
    filename: str, name: str, expected_kind: str
) -> None:
    memberships = parse_block_file(_block_file(name, 7, ("600000",)), filename)

    assert len(memberships) == 1
    assert memberships[0].membership_kind == expected_kind
    assert memberships[0].code == "600000"


def test_chunk_assembly_handles_boundaries_without_duplicate_memberships() -> None:
    data = _block_file("主题板块", 2, ("600000", "600000", "300001"))
    chunks = (data[:401], data[401:406], data[406:])

    memberships = parse_block_file(assemble_block_chunks(chunks, len(data)), "block_gn.dat")

    assert {(item.market, item.code) for item in memberships} == {(1, "600000"), (0, "300001")}
    assert {item.membership_kind for item in memberships} == {"ThemeMembership"}


def test_block_parser_skips_each_fixed_member_slot_before_the_next_block() -> None:
    data = (
        b"\x00" * 384
        + struct.pack("<H", 2)
        + _block_record("第一块", 1, ("600000",))
        + _block_record("第二块", 2, ("300001",))
    )

    memberships = parse_block_file(data, "block_gn.dat")

    assert [(item.block_name, item.code) for item in memberships] == [
        ("第一块", "600000"),
        ("第二块", "300001"),
    ]


def test_block_parser_rejects_short_or_overlong_chunk_streams() -> None:
    data = _block_file("测试", 1, ("600000",))

    with pytest.raises(TdxBlockParseError, match="length"):
        assemble_block_chunks((data[:-1],), len(data))
    with pytest.raises(TdxBlockParseError, match="length"):
        assemble_block_chunks((data, b"x"), len(data))
    with pytest.raises(TdxBlockParseError, match="truncated"):
        parse_block_file(data[:-1], "block_gn.dat")
