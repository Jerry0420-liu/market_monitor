"""Onboard Owner-approved TDX Concept/Theme views as P0 sector subjects."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339

from market_monitor_data.reference import ReferenceRepository

_SH_PREFIXES = frozenset({"600", "601", "603", "605", "688"})
_SZ_PREFIXES = frozenset({"000", "001", "002", "003", "300", "301"})
_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


class TdxSectorOnboarding:
    """Freeze only primary A-share members from native TDX Concept/Theme blocks."""

    def __init__(self, runtime: DatabaseRuntime, references: ReferenceRepository) -> None:
        self._runtime = runtime
        self._references = references

    def synchronize(self, epoch_uid: str, observed_at: datetime) -> tuple[str, ...]:
        groups: dict[tuple[str, str, str, int, str], set[str]] = defaultdict(set)
        instant = format_rfc3339(observed_at)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "WITH active_mapping AS ("
                "SELECT external_code,instrument_uid,"
                "row_number() OVER (PARTITION BY external_code "
                "ORDER BY valid_from DESC) AS ordinal "
                "FROM provider_mapping WHERE provider_key='NATIVE_TDX' "
                "AND mapping_status='RESOLVED' AND valid_from<=? "
                "AND (valid_until IS NULL OR valid_until>?)"
                "), latest_block AS ("
                "SELECT block_version_uid,filename,"
                "row_number() OVER (PARTITION BY filename ORDER BY version DESC) AS ordinal "
                "FROM tdx_block_artifact_version WHERE fetched_at<=?"
                ") "
                "SELECT b.block_version_uid,a.filename,b.block_name,b.block_type,"
                "b.membership_kind,b.market,b.external_code,m.instrument_uid,i.instrument_kind "
                "FROM tdx_block_membership b "
                "JOIN tdx_block_artifact_version a ON a.block_version_uid=b.block_version_uid "
                "JOIN latest_block latest ON latest.block_version_uid=a.block_version_uid "
                "AND latest.ordinal=1 "
                "JOIN active_mapping m ON m.external_code=printf('%d:%s',b.market,b.external_code) "
                "AND m.ordinal=1 "
                "JOIN instrument i ON i.instrument_uid=m.instrument_uid "
                "WHERE b.membership_kind IN ('ConceptMembership','ThemeMembership') "
                "ORDER BY a.filename,b.membership_kind,b.block_type,b.block_name,m.instrument_uid",
                (instant, instant, instant),
            ).all()
        for row in rows:
            if not _is_primary_a_share(
                str(row.instrument_kind), int(row.market), str(row.external_code)
            ):
                continue
            groups[
                (
                    str(row.block_version_uid),
                    str(row.filename),
                    str(row.membership_kind),
                    int(row.block_type),
                    str(row.block_name),
                )
            ].add(str(row.instrument_uid))

        trading_date = observed_at.astimezone(_SHANGHAI).date().isoformat()
        created: list[str] = []
        for key, members in sorted(groups.items()):
            block_version_uid, filename, membership_kind, block_type, block_name = key
            sector_uid = self._sector_uid(
                filename, membership_kind, block_type, block_name, observed_at
            )
            latest = self._latest_membership(sector_uid, trading_date)
            if latest is not None and str(latest.block_version_uid) == block_version_uid:
                continue
            membership_version_uid = self._references.freeze_membership(
                sector_uid,
                trading_date,
                sorted(members),
                observed_at,
                correction=latest is not None,
            )
            self._references.record_sector_membership_source(
                membership_version_uid,
                membership_kind,
                block_name,
                block_type,
                block_version_uid,
            )
            created.append(membership_version_uid)
        return tuple(created)

    def _sector_uid(
        self,
        filename: str,
        membership_kind: str,
        block_type: int,
        block_name: str,
        observed_at: datetime,
    ) -> str:
        external_code = f"TDX:{filename}:{membership_kind}:{block_type}:{block_name}"
        sector_uid = self._references.resolve_provider_mapping(
            "NATIVE_TDX", external_code, observed_at
        )
        if sector_uid is not None:
            self._references.ensure_analysis_subject("SECTOR", sector_uid)
            return sector_uid
        sector_uid = self._references.create_sector("CONCEPT", observed_at)
        self._references.add_sector_version(sector_uid, block_name, observed_at)
        self._references.map_sector("NATIVE_TDX", external_code, sector_uid, observed_at)
        self._references.ensure_analysis_subject("SECTOR", sector_uid)
        return sector_uid

    def _latest_membership(self, sector_uid: str, trading_date: str) -> Any | None:
        with self._runtime.read_connection() as connection:
            return connection.exec_driver_sql(
                "SELECT v.membership_version_uid,s.block_version_uid "
                "FROM sector_membership_version v "
                "LEFT JOIN tdx_sector_membership_source s "
                "ON s.membership_version_uid=v.membership_version_uid "
                "WHERE v.sector_uid=? AND v.trading_date=? "
                "ORDER BY v.version DESC LIMIT 1",
                (sector_uid, trading_date),
            ).one_or_none()


def _is_primary_a_share(instrument_kind: str, market: int, code: str) -> bool:
    if instrument_kind != "STOCK":
        return False
    if market == 1:
        return code[:3] in _SH_PREFIXES
    return market == 0 and code[:3] in _SZ_PREFIXES
