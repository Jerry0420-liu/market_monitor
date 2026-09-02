import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid, parse_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.canonical import canonical_bytes, canonical_hash

_SQL_READ_BATCH_SIZE = 500


def _rows_for_identifiers(connection: Any, statement: str, identifiers: Sequence[str]) -> list[Any]:
    rows: list[Any] = []
    for offset in range(0, len(identifiers), _SQL_READ_BATCH_SIZE):
        batch = identifiers[offset : offset + _SQL_READ_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(
            connection.exec_driver_sql(
                statement.format(placeholders=placeholders), tuple(batch)
            ).all()
        )
    return rows


class SnapshotError(RuntimeError):
    pass


class FutureInputError(SnapshotError):
    pass


class RequiredCapabilityError(SnapshotError):
    pass


class SnapshotSkewError(SnapshotError):
    pass


class SnapshotFreshnessError(SnapshotError):
    pass


@dataclass(frozen=True)
class PrimaryQuoteStatus:
    """One Primary instrument's canonical quote state in a version-2 manifest."""

    instrument_uid: str
    quote_uid: str | None
    status: Literal["VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"]

    def __post_init__(self) -> None:
        if not self.instrument_uid:
            raise ValueError("primary quote status requires an instrument UID")
        if self.status not in {"VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"}:
            raise ValueError("unknown primary quote status")
        if (self.status == "VALID") != (self.quote_uid is not None):
            raise ValueError("only VALID primary quote status may have a quote UID")
        if self.quote_uid is not None and not self.quote_uid:
            raise ValueError("primary quote status quote UID must not be empty")


class SnapshotBuilder:
    def __init__(
        self, runtime: DatabaseRuntime, writer: WriterQueue, artifacts: ArtifactStore
    ) -> None:
        self._runtime = runtime
        self._writer = writer
        self._artifacts = artifacts

    def create_reference_bundle(self, entries: list[tuple[str, str, str]]) -> str:
        normalized = sorted(set(entries))
        if not normalized or any(not all(item) for item in normalized):
            raise ValueError("reference bundle entries must be non-empty stable identifiers")
        with self._runtime.read_connection() as connection:
            for kind, entity_uid, version_uid in normalized:
                if not _reference_entry_exists(connection, kind, entity_uid, version_uid):
                    raise LookupError(f"unknown {kind} reference version: {version_uid}")
        digest = canonical_hash(normalized)
        existing = self._scalar(
            "SELECT bundle_uid FROM reference_version_bundle WHERE canonical_hash=?", (digest,)
        )
        if existing is not None:
            return existing
        uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO reference_version_bundle(bundle_uid,canonical_hash,created_at) "
                "VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (uid, digest),
            )
            for kind, entity_uid, version_uid in normalized:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO reference_version_entry"
                    "(bundle_uid,entity_kind,entity_uid,version_uid) VALUES (?,?,?,?)",
                    (uid, kind, entity_uid, version_uid),
                )

        self._writer.submit(command).result()
        return uid

    def create_manifest(
        self,
        quote_uids: list[str],
        as_of: datetime,
        *,
        bar_uids: Sequence[str] = (),
        context_quote_uids: Sequence[str] | None = None,
        realtime_quote_uids: Sequence[str] | None = None,
        realtime_current_bar_uids: Sequence[str] | None = None,
        realtime_minute_cohort: Mapping[str, Any] | None = None,
        shadow_round_uid: str | None = None,
        primary_quote_statuses: Sequence[PrimaryQuoteStatus] = (),
        historical_windows: Mapping[str, Sequence[str]] | None = None,
    ) -> str:
        instant = format_rfc3339(as_of)
        normalized = sorted(set(quote_uids))
        if not normalized:
            raise ValueError("manifest requires at least one quote")
        if shadow_round_uid is not None and (
            not isinstance(shadow_round_uid, str) or not shadow_round_uid
        ):
            raise ValueError("shadow round UID must be a non-empty string")
        normalized_context_quotes = sorted(set(context_quote_uids or ()))
        if set(normalized) & set(normalized_context_quotes):
            raise ValueError("context quote UIDs must not duplicate Primary quote UIDs")
        normalized_bars = sorted(set(bar_uids))
        normalized_realtime_quotes = sorted(set(realtime_quote_uids or ()))
        normalized_realtime_bars = sorted(set(realtime_current_bar_uids or ()))
        statuses = _normalize_primary_quote_statuses(primary_quote_statuses)
        windows = _normalize_historical_windows(historical_windows)
        is_version_three = context_quote_uids is not None
        is_version_five = realtime_minute_cohort is not None
        is_version_four = (
            realtime_quote_uids is not None
            or realtime_current_bar_uids is not None
            or is_version_five
        )
        is_version_two = bool(
            normalized_bars
            or statuses
            or historical_windows is not None
            or is_version_three
            or is_version_four
        )
        all_quote_uids = [*normalized, *normalized_context_quotes]
        if is_version_four:
            if realtime_quote_uids is None or realtime_current_bar_uids is None:
                raise ValueError("realtime quote and current bar UIDs must be supplied together")
            if set(normalized_realtime_quotes) != set(all_quote_uids):
                raise ValueError("realtime quote UIDs must include every live manifest quote")
            if not normalized_realtime_bars:
                raise ValueError("realtime current bar UIDs must not be empty")
            if not set(normalized_realtime_bars).issubset(normalized_bars):
                raise ValueError("realtime current bar UIDs must belong to manifest bars")
        minute_cohort = _normalize_realtime_minute_cohort(
            realtime_minute_cohort,
            normalized_realtime_bars,
            statuses,
        )
        with self._runtime.read_connection() as connection:
            rows = _rows_for_identifiers(
                connection,
                "SELECT q.quote_uid,q.received_at,l.instrument_uid FROM market_quote q "
                "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
                "WHERE q.quote_uid IN ({placeholders}) ORDER BY q.quote_uid",
                all_quote_uids,
            )
            _validate_manifest_bars(connection, normalized_bars, instant)
            if is_version_four:
                _validate_realtime_current_bars(connection, normalized_realtime_bars)
        if len(rows) != len(all_quote_uids):
            raise LookupError("manifest contains an unknown quote UID")
        if any(str(row.received_at) > instant for row in rows):
            raise FutureInputError("manifest cannot include a quote received after as-of time")
        with self._runtime.read_connection() as connection:
            _validate_primary_quote_statuses(
                connection,
                statuses,
                [row for row in rows if str(row.quote_uid) in set(normalized)],
            )
        document: dict[str, Any] = {"as_of_time": instant, "quote_uids": normalized}
        if is_version_two:
            document.update(
                {
                    "schema_version": (
                        5
                        if is_version_five
                        else 4
                        if is_version_four
                        else 3
                        if is_version_three
                        else 2
                    ),
                    "bar_uids": normalized_bars,
                    "primary_quote_statuses": [
                        {
                            "instrument_uid": item.instrument_uid,
                            "quote_uid": item.quote_uid,
                            "status": item.status,
                        }
                        for item in statuses
                    ],
                    "historical_windows": windows,
                }
            )
        if is_version_three or is_version_four:
            document["context_quote_uids"] = normalized_context_quotes
        if is_version_four:
            document["realtime_quote_uids"] = normalized_realtime_quotes
            document["realtime_current_bar_uids"] = normalized_realtime_bars
        if minute_cohort is not None:
            document["realtime_minute_cohort"] = minute_cohort
        if shadow_round_uid is not None:
            document["shadow_round_uid"] = shadow_round_uid
        digest = canonical_hash(document)
        existing = self._scalar(
            "SELECT manifest_uid FROM input_manifest WHERE canonical_hash=?", (digest,)
        )
        if existing is not None:
            return existing
        artifact = self._artifacts.put_bytes(
            canonical_bytes(document), "application/vnd.market-monitor.input-manifest+json"
        )
        self._artifacts.register(artifact)
        uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            current_rows = _rows_for_identifiers(
                transaction.connection,
                "SELECT q.quote_uid,q.received_at,l.instrument_uid FROM market_quote q "
                "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
                "WHERE q.quote_uid IN ({placeholders}) ORDER BY q.quote_uid",
                all_quote_uids,
            )
            if len(current_rows) != len(all_quote_uids):
                raise LookupError("manifest contains an unknown quote UID")
            if any(str(row.received_at) > instant for row in current_rows):
                raise FutureInputError("manifest cannot include a quote received after as-of time")
            _validate_manifest_bars(transaction.connection, normalized_bars, instant)
            if is_version_four:
                _validate_realtime_current_bars(transaction.connection, normalized_realtime_bars)
            _validate_primary_quote_statuses(
                transaction.connection,
                statuses,
                [row for row in current_rows if str(row.quote_uid) in set(normalized)],
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO input_manifest"
                "(manifest_uid,canonical_hash,artifact_sha256,as_of_time,created_at) "
                "VALUES (?,?,?,?,?)",
                (uid, digest, artifact.sha256, instant, instant),
            )

        self._writer.submit(command).result()
        return uid

    @staticmethod
    def manifest_schema_version(document: dict[str, Any]) -> int:
        value = document.get("schema_version", 1)
        if not isinstance(value, int) or isinstance(value, bool) or value not in {1, 2, 3, 4, 5}:
            raise SnapshotError("unsupported manifest schema version")
        return value

    def create_snapshot(
        self,
        subject_uid: str,
        manifest_uid: str,
        bundle_uid: str,
        disposition: str,
        required_capabilities: list[str],
        optional_capabilities: list[str],
        *,
        max_skew_ms: int,
        max_age_ms: int | None = None,
    ) -> str:
        if max_skew_ms < 0:
            raise ValueError("max_skew_ms must be non-negative")
        if max_age_ms is not None and max_age_ms < 0:
            raise ValueError("max_age_ms must be non-negative")
        required = sorted(set(required_capabilities))
        optional = sorted(set(optional_capabilities) - set(required))
        manifest_document = self.replay_manifest(manifest_uid)
        schema_version = self.manifest_schema_version(manifest_document)
        quote_uids = manifest_document.get("quote_uids")
        if not isinstance(quote_uids, list) or not all(
            isinstance(value, str) for value in quote_uids
        ):
            raise SnapshotError("manifest quote_uids are invalid")
        bar_uids: list[str] = []
        context_quote_uids: list[str] = []
        realtime_quote_uids: list[str] = []
        realtime_current_bar_uids: list[str] = []
        realtime_minute_cohort: dict[str, Any] | None = None
        replay_primary_statuses: tuple[PrimaryQuoteStatus, ...] = ()
        if schema_version in {2, 3, 4, 5}:
            value = manifest_document.get("bar_uids")
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise SnapshotError("manifest bar_uids are invalid")
            bar_uids = value
            _validate_version_two_replay_manifest(manifest_document, quote_uids)
            replay_primary_statuses = tuple(
                PrimaryQuoteStatus(
                    str(item["instrument_uid"]),
                    str(item["quote_uid"]) if item["quote_uid"] is not None else None,
                    cast(
                        Literal["VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"], item["status"]
                    ),
                )
                for item in manifest_document["primary_quote_statuses"]
            )
        if schema_version in {3, 4, 5}:
            context_quote_uids = _replay_context_quote_uids(manifest_document, quote_uids)
        all_quote_uids = [*quote_uids, *context_quote_uids]
        if schema_version in {4, 5}:
            if max_age_ms is None:
                raise SnapshotError("realtime manifest requires an explicit max_age_ms")
            realtime_quote_uids = _replay_realtime_quote_uids(manifest_document, all_quote_uids)
            realtime_current_bar_uids = _replay_realtime_current_bar_uids(
                manifest_document, bar_uids
            )
        if schema_version == 5:
            realtime_minute_cohort = _replay_realtime_minute_cohort(
                manifest_document,
                realtime_current_bar_uids,
                replay_primary_statuses,
            )
        with self._runtime.read_connection() as connection:
            manifest = connection.exec_driver_sql(
                "SELECT as_of_time FROM input_manifest WHERE manifest_uid=?", (manifest_uid,)
            ).one()
            quote_rows = _rows_for_identifiers(
                connection,
                "SELECT q.quote_uid,q.source_time,l.epoch_uid,l.instrument_uid FROM market_quote q "
                "JOIN quote_lineage l ON l.lineage_uid=q.lineage_uid "
                "WHERE q.quote_uid IN ({placeholders})",
                all_quote_uids,
            )
            if replay_primary_statuses:
                try:
                    _validate_primary_quote_statuses(
                        connection,
                        replay_primary_statuses,
                        [row for row in quote_rows if str(row.quote_uid) in set(quote_uids)],
                    )
                except (LookupError, ValueError) as error:
                    raise SnapshotError(str(error)) from error
            bar_rows: list[Any] = []
            if bar_uids:
                bar_rows = _rows_for_identifiers(
                    connection,
                    "SELECT b.bar_uid,b.source_time,b.epoch_uid,b.interval_kind,b.instrument_uid,"
                    "(SELECT CAST(substr(m.external_code,1,"
                    "instr(m.external_code,':')-1) AS INTEGER) "
                    "FROM provider_mapping m JOIN market_source_epoch e "
                    "ON e.provider_key=m.provider_key WHERE e.epoch_uid=b.epoch_uid "
                    "AND m.instrument_uid=b.instrument_uid AND m.entity_kind='INSTRUMENT' "
                    "AND m.mapping_status='RESOLVED' AND m.valid_from<=e.started_at "
                    "AND (m.valid_until IS NULL OR m.valid_until>e.started_at) "
                    "ORDER BY m.valid_from DESC LIMIT 1) AS tdx_market FROM tdx_bar b "
                    "WHERE b.bar_uid IN ({placeholders})",
                    bar_uids,
                )
                if len(bar_rows) != len(bar_uids):
                    raise SnapshotError("manifest contains an unknown bar UID")
            bundle_exists = connection.exec_driver_sql(
                "SELECT count(*) FROM reference_version_bundle WHERE bundle_uid=?", (bundle_uid,)
            ).scalar_one()
        if not bundle_exists:
            raise LookupError("reference bundle does not exist")
        as_of = str(manifest.as_of_time)
        epochs = sorted({str(row.epoch_uid) for row in [*quote_rows, *bar_rows]})
        if len(epochs) != 1:
            raise SnapshotError("one snapshot currently requires exactly one source epoch")
        realtime_rows = [*quote_rows, *bar_rows]
        if schema_version in {4, 5}:
            quote_by_uid = {str(row.quote_uid): row for row in quote_rows}
            bar_by_uid = {str(row.bar_uid): row for row in bar_rows}
            if any(str(bar_by_uid[uid].interval_kind) != "1m" for uid in realtime_current_bar_uids):
                raise SnapshotError("realtime current bars must be 1m")
            snapshot_time = parse_rfc3339(as_of)
            if any(
                parse_rfc3339(str(row.source_time)) > snapshot_time for row in quote_by_uid.values()
            ):
                raise SnapshotFreshnessError("realtime quote source time is after as-of")
            if any(
                parse_rfc3339(str(bar_by_uid[uid].source_time)) + timedelta(minutes=1)
                > snapshot_time
                for uid in realtime_current_bar_uids
            ):
                raise SnapshotFreshnessError("realtime current bar is not complete at as-of")
            if realtime_minute_cohort is not None:
                _validate_realtime_minute_cohort_bars(
                    bar_by_uid,
                    realtime_current_bar_uids,
                    realtime_minute_cohort,
                    replay_primary_statuses,
                )
            realtime_rows = [
                *(quote_by_uid[uid] for uid in realtime_quote_uids),
                *(bar_by_uid[uid] for uid in realtime_current_bar_uids),
            ]
        if schema_version in {4, 5}:
            source_times = [
                *(parse_rfc3339(str(row.source_time)) for row in quote_by_uid.values()),
                *(
                    parse_rfc3339(str(row.source_time)) + timedelta(minutes=1)
                    for row in bar_by_uid.values()
                    if str(row.bar_uid) in set(realtime_current_bar_uids)
                ),
            ]
        else:
            source_times = [
                parse_rfc3339(str(row.source_time)) for row in realtime_rows if row.source_time
            ]
        if source_times:
            skew_ms = int((max(source_times) - min(source_times)).total_seconds() * 1000)
            if skew_ms > max_skew_ms:
                raise SnapshotSkewError("quote source-time skew exceeds the request limit")
            observed_max_age_ms = max(
                0,
                int((parse_rfc3339(as_of) - min(source_times)).total_seconds() * 1000),
            )
            if max_age_ms is not None and observed_max_age_ms > max_age_ms:
                raise SnapshotFreshnessError("realtime source age exceeds request limit")
        else:
            observed_max_age_ms = 0
        captures = [
            self._capture_capability(epochs[0], capability, as_of, capability in required)
            for capability in [*required, *optional]
        ]
        failed = [
            item[0]
            for item in captures
            if item[6] and (item[2] in {"UNKNOWN", "UNHEALTHY"} or item[3] in {"UNKNOWN", "UNFIT"})
        ]
        if failed:
            raise RequiredCapabilityError(
                f"required capabilities are unavailable: {', '.join(failed)}"
            )
        quality = _aggregate_quality(captures)
        document = {
            "subject_uid": subject_uid,
            "manifest_uid": manifest_uid,
            "bundle_uid": bundle_uid,
            "as_of_time": as_of,
            "max_skew_ms": max_skew_ms,
            "evaluation_disposition": disposition,
            "capabilities": captures,
        }
        if max_age_ms is not None:
            document["max_age_ms"] = max_age_ms
        digest = canonical_hash(document)
        existing = self._scalar(
            "SELECT snapshot_uid FROM evaluation_snapshot WHERE canonical_hash=?", (digest,)
        )
        if existing is not None:
            return existing
        quality_uid = new_uid()
        snapshot_uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO quality_context"
                "(quality_context_uid,data_health_status,fitness_status,evidence_sufficiency,"
                "coverage_ppm,max_age_ms,observed_at,valid_until) VALUES (?,?,?,?,?,?,?,?)",
                (quality_uid, *quality, observed_max_age_ms, as_of, as_of),
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO evaluation_snapshot"
                "(snapshot_uid,subject_uid,manifest_uid,bundle_uid,quality_context_uid,as_of_time,"
                "max_skew_ms,evaluation_disposition,snapshot_status,canonical_hash,created_at,"
                "sealed_at) "
                "VALUES (?,?,?,?,?,?,?,?,'DRAFT',?,?,NULL)",
                (
                    snapshot_uid,
                    subject_uid,
                    manifest_uid,
                    bundle_uid,
                    quality_uid,
                    as_of,
                    max_skew_ms,
                    disposition,
                    digest,
                    as_of,
                ),
            )
            for capability, report_uid, health, fitness, coverage, latency, is_required in captures:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO capability_snapshot"
                    "(snapshot_uid,capability,epoch_uid,health_report_uid,health_status,fitness_status,"
                    "coverage_ppm,latency_ms,required) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_uid,
                        capability,
                        epochs[0],
                        report_uid,
                        health,
                        fitness,
                        coverage,
                        latency,
                        int(is_required),
                    ),
                )

        self._writer.submit(command).result()
        return snapshot_uid

    def seal(self, snapshot_uid: str) -> None:
        def command(transaction: TransactionContext) -> None:
            row = transaction.connection.exec_driver_sql(
                "SELECT snapshot_status,as_of_time FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uid,),
            ).one()
            if row.snapshot_status == "SEALED":
                return
            if row.snapshot_status != "DRAFT":
                raise SnapshotError("only a draft snapshot can be sealed")
            transaction.connection.exec_driver_sql(
                "UPDATE evaluation_snapshot SET snapshot_status='SEALED',sealed_at=? "
                "WHERE snapshot_uid=?",
                (row.as_of_time, snapshot_uid),
            )

        self._writer.submit(command).result()

    def replay_manifest(self, manifest_uid: str) -> dict[str, Any]:
        digest = self._scalar(
            "SELECT artifact_sha256 FROM input_manifest WHERE manifest_uid=?", (manifest_uid,)
        )
        if digest is None:
            raise LookupError(manifest_uid)
        with self._artifacts.open_verified(digest) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise SnapshotError("manifest Artifact is not an object")
        return value

    def _capture_capability(
        self, epoch_uid: str, capability: str, as_of: str, required: bool
    ) -> tuple[str, str | None, str, str, int, int, bool]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT report_uid,health_status,fitness_status,coverage_ppm,latency_ms,"
                "valid_until "
                "FROM capability_health_report WHERE epoch_uid=? AND capability=? "
                "AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                (epoch_uid, capability, as_of),
            ).one_or_none()
        if row is None or str(row.valid_until) < as_of:
            return capability, None, "UNKNOWN", "UNKNOWN", 0, 0, required
        return (
            capability,
            str(row.report_uid),
            str(row.health_status),
            str(row.fitness_status),
            int(row.coverage_ppm),
            int(row.latency_ms),
            required,
        )

    def _scalar(self, sql: str, parameters: tuple[object, ...]) -> str | None:
        with self._runtime.read_connection() as connection:
            value = connection.exec_driver_sql(sql, parameters).scalar_one_or_none()
        return None if value is None else str(value)


def _normalize_primary_quote_statuses(
    statuses: Sequence[PrimaryQuoteStatus],
) -> tuple[PrimaryQuoteStatus, ...]:
    if not all(isinstance(item, PrimaryQuoteStatus) for item in statuses):
        raise TypeError("primary quote statuses must be PrimaryQuoteStatus values")
    normalized = tuple(sorted(statuses, key=lambda item: item.instrument_uid))
    if len({item.instrument_uid for item in normalized}) != len(normalized):
        raise ValueError("primary quote statuses must not duplicate an instrument")
    return normalized


def _normalize_historical_windows(
    windows: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    if windows is None:
        return {}
    normalized: dict[str, list[str]] = {}
    for name in sorted(windows):
        values = windows[name]
        if not isinstance(name, str) or not name or isinstance(values, str):
            raise ValueError("historical windows require named string identities")
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("historical window identities must be non-empty strings")
        ordered = sorted(values)
        if len(set(ordered)) != len(ordered):
            raise ValueError("historical window identities must not duplicate")
        normalized[name] = ordered
    return normalized


def _validate_manifest_bars(connection: Any, bar_uids: Sequence[str], instant: str) -> None:
    if not bar_uids:
        return
    rows = _rows_for_identifiers(
        connection,
        "SELECT bar_uid,source_time FROM tdx_bar "
        "WHERE bar_uid IN ({placeholders}) ORDER BY bar_uid",
        bar_uids,
    )
    if len(rows) != len(bar_uids):
        raise LookupError("manifest contains an unknown bar UID")
    if any(str(row.source_time) > instant for row in rows):
        raise FutureInputError("manifest cannot include a bar after as-of time")


def _validate_realtime_current_bars(connection: Any, bar_uids: Sequence[str]) -> None:
    rows = _rows_for_identifiers(
        connection,
        "SELECT bar_uid,interval_kind FROM tdx_bar "
        "WHERE bar_uid IN ({placeholders}) ORDER BY bar_uid",
        bar_uids,
    )
    if len(rows) != len(bar_uids) or any(str(row.interval_kind) != "1m" for row in rows):
        raise ValueError("realtime current bar UIDs must identify 1m canonical bars")


def _validate_primary_quote_statuses(
    connection: Any,
    statuses: Sequence[PrimaryQuoteStatus],
    quote_rows: Sequence[Any],
) -> None:
    if not statuses:
        return
    known = {
        str(row.instrument_uid)
        for row in _rows_for_identifiers(
            connection,
            "SELECT instrument_uid FROM instrument WHERE instrument_uid IN ({placeholders})",
            [item.instrument_uid for item in statuses],
        )
    }
    if known != {item.instrument_uid for item in statuses}:
        raise LookupError("primary quote status contains an unknown instrument UID")
    quote_instruments = {str(row.quote_uid): str(row.instrument_uid) for row in quote_rows}
    status_quotes = {item.quote_uid for item in statuses if item.quote_uid is not None}
    if status_quotes != set(quote_instruments):
        raise ValueError("version-2 primary statuses must represent every manifest quote")
    for item in statuses:
        if item.quote_uid is not None and quote_instruments[item.quote_uid] != item.instrument_uid:
            raise ValueError("VALID primary quote status must match the quote instrument")


def _replay_context_quote_uids(
    document: dict[str, Any], primary_quote_uids: Sequence[str]
) -> list[str]:
    value = document.get("context_quote_uids")
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
        or set(value) & set(primary_quote_uids)
    ):
        raise SnapshotError("manifest context_quote_uids are invalid")
    return sorted(value)


def _replay_realtime_quote_uids(
    document: dict[str, Any], all_quote_uids: Sequence[str]
) -> list[str]:
    value = document.get("realtime_quote_uids")
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or value != sorted(value)
        or set(value) != set(all_quote_uids)
    ):
        raise SnapshotError("manifest realtime quote UIDs are invalid")
    return value


def _replay_realtime_current_bar_uids(
    document: dict[str, Any], bar_uids: Sequence[str]
) -> list[str]:
    value = document.get("realtime_current_bar_uids")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or value != sorted(value)
        or not set(value).issubset(bar_uids)
    ):
        raise SnapshotError("manifest realtime current bar UIDs are invalid")
    return value


def _normalize_realtime_minute_cohort(
    value: Mapping[str, Any] | None,
    realtime_current_bar_uids: Sequence[str],
    statuses: Sequence[PrimaryQuoteStatus],
) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {
        "targets",
        "expected_primary_count",
        "known_suspended_count",
        "valid_latest_complete_count",
        "missing_or_invalid_count",
        "coverage_ppm",
        "source_time_integrity",
        "request_count",
        "worker_count",
        "duration_ms",
        "failure_counts",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("realtime minute cohort schema is invalid")
    targets = value["targets"]
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)) or not targets:
        raise ValueError("realtime minute cohort targets are invalid")
    normalized_targets: list[dict[str, Any]] = []
    seen_markets: set[int] = set()
    for target in targets:
        if not isinstance(target, Mapping):
            raise ValueError("realtime minute cohort targets are invalid")
        market = target.get("market")
        source_time = target.get("source_time")
        if (
            not isinstance(market, int)
            or isinstance(market, bool)
            or market not in {0, 1}
            or not isinstance(source_time, str)
            or market in seen_markets
        ):
            raise ValueError("realtime minute cohort targets are invalid")
        try:
            parsed = parse_rfc3339(source_time)
        except ValueError as error:
            raise ValueError("realtime minute cohort targets are invalid") from error
        if format_rfc3339(parsed) != source_time:
            raise ValueError("realtime minute cohort targets must be canonical timestamps")
        seen_markets.add(market)
        normalized_targets.append({"market": market, "source_time": source_time})
    normalized_targets.sort(key=lambda target: int(target["market"]))

    integers = {
        name: value[name]
        for name in (
            "expected_primary_count",
            "known_suspended_count",
            "valid_latest_complete_count",
            "missing_or_invalid_count",
            "coverage_ppm",
            "request_count",
            "worker_count",
            "duration_ms",
        )
    }
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 0
        for number in integers.values()
    ):
        raise ValueError("realtime minute cohort counts are invalid")
    expected = integers["expected_primary_count"]
    suspended = integers["known_suspended_count"]
    valid = integers["valid_latest_complete_count"]
    missing = integers["missing_or_invalid_count"]
    tradable = expected - suspended
    if tradable < 0 or valid + missing != tradable:
        raise ValueError("realtime minute cohort counts are inconsistent")
    coverage = 0 if tradable == 0 else valid * 1_000_000 // tradable
    if integers["coverage_ppm"] != coverage or valid != len(realtime_current_bar_uids):
        raise ValueError("realtime minute cohort coverage is inconsistent")
    if statuses and (
        expected != len(statuses)
        or suspended != sum(item.status == "SUSPENDED" for item in statuses)
    ):
        raise ValueError("realtime minute cohort quote status denominator is inconsistent")
    if value["source_time_integrity"] != "EXACT_TARGET":
        raise ValueError("realtime minute cohort source-time integrity is invalid")
    failures = value["failure_counts"]
    if not isinstance(failures, Mapping) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for name, count in failures.items()
    ):
        raise ValueError("realtime minute cohort failure counts are invalid")
    return {
        "targets": normalized_targets,
        **integers,
        "source_time_integrity": "EXACT_TARGET",
        "failure_counts": {name: failures[name] for name in sorted(failures)},
    }


def _replay_realtime_minute_cohort(
    document: dict[str, Any],
    realtime_current_bar_uids: Sequence[str],
    statuses: Sequence[PrimaryQuoteStatus],
) -> dict[str, Any]:
    try:
        cohort = _normalize_realtime_minute_cohort(
            document.get("realtime_minute_cohort"),
            realtime_current_bar_uids,
            statuses,
        )
    except ValueError as error:
        raise SnapshotError(str(error)) from error
    if cohort is None:
        raise SnapshotError("manifest realtime minute cohort is missing")
    return cohort


def _validate_realtime_minute_cohort_bars(
    bar_by_uid: Mapping[str, Any],
    realtime_current_bar_uids: Sequence[str],
    cohort: Mapping[str, Any],
    statuses: Sequence[PrimaryQuoteStatus],
) -> None:
    targets = {
        int(target["market"]): parse_rfc3339(str(target["source_time"]))
        for target in cohort["targets"]
    }
    instrument_uids = [str(bar_by_uid[uid].instrument_uid) for uid in realtime_current_bar_uids]
    if (
        len(set(instrument_uids)) != len(instrument_uids)
        or not set(instrument_uids).issubset({item.instrument_uid for item in statuses})
        or any(
            row.tdx_market is None
            or int(row.tdx_market) not in targets
            or parse_rfc3339(str(row.source_time)) != targets[int(row.tdx_market)]
            for row in (bar_by_uid[uid] for uid in realtime_current_bar_uids)
        )
    ):
        raise SnapshotFreshnessError(
            "realtime current bar does not cover the legal minute cohort for its market target"
        )


def _validate_version_two_replay_manifest(document: dict[str, Any], quote_uids: list[str]) -> None:
    if len(set(quote_uids)) != len(quote_uids):
        raise SnapshotError("manifest quote_uids must not duplicate")
    statuses = document.get("primary_quote_statuses")
    if not isinstance(statuses, list):
        raise SnapshotError("manifest primary_quote_statuses are invalid")
    instrument_uids: set[str] = set()
    valid_quote_uids: set[str] = set()
    for item in statuses:
        if not isinstance(item, dict):
            raise SnapshotError("manifest primary_quote_statuses are invalid")
        instrument_uid = item.get("instrument_uid")
        quote_uid = item.get("quote_uid")
        status = item.get("status")
        if (
            not isinstance(instrument_uid, str)
            or not instrument_uid
            or status not in {"VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"}
            or (status == "VALID") != isinstance(quote_uid, str)
        ):
            raise SnapshotError("manifest primary_quote_statuses are invalid")
        if instrument_uid in instrument_uids:
            raise SnapshotError("manifest primary_quote_statuses must not duplicate an instrument")
        instrument_uids.add(instrument_uid)
        if isinstance(quote_uid, str):
            valid_quote_uids.add(quote_uid)
    if valid_quote_uids != set(quote_uids):
        raise SnapshotError("manifest primary_quote_statuses must represent every quote")
    historical_windows = document.get("historical_windows")
    if not isinstance(historical_windows, dict):
        raise SnapshotError("manifest historical_windows are invalid")
    for name, values in historical_windows.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(values, list)
            or not all(isinstance(value, str) and value for value in values)
            or len(set(values)) != len(values)
        ):
            raise SnapshotError("manifest historical_windows are invalid")


def _aggregate_quality(
    captures: list[tuple[str, str | None, str, str, int, int, bool]],
) -> tuple[str, str, str, int]:
    if not captures:
        return "UNKNOWN", "UNKNOWN", "UNKNOWN", 0
    health_order = {"HEALTHY": 0, "DEGRADED": 1, "UNKNOWN": 2, "UNHEALTHY": 3}
    fitness_order = {"FIT": 0, "FIT_WITH_LIMITATIONS": 1, "UNKNOWN": 2, "UNFIT": 3}
    health = max((item[2] for item in captures), key=health_order.__getitem__)
    fitness = max((item[3] for item in captures), key=fitness_order.__getitem__)
    coverage = min(item[4] for item in captures)
    evidence = "HIGH" if health == "HEALTHY" and fitness == "FIT" else "MEDIUM"
    return health, fitness, evidence, coverage


def _reference_entry_exists(connection: Any, kind: str, entity_uid: str, version_uid: str) -> bool:
    queries = {
        "INSTRUMENT": (
            "SELECT count(*) FROM instrument_identity_version "
            "WHERE instrument_uid=? AND identity_version_uid=?",
            (entity_uid, version_uid),
        ),
        "SECTOR": (
            "SELECT count(*) FROM sector_version WHERE sector_uid=? AND sector_version_uid=?",
            (entity_uid, version_uid),
        ),
        "MEMBERSHIP": (
            "SELECT count(*) FROM sector_membership_version "
            "WHERE sector_uid=? AND membership_version_uid=?",
            (entity_uid, version_uid),
        ),
        "MAPPING": (
            "SELECT (SELECT count(*) FROM provider_mapping WHERE mapping_uid=? "
            "AND (instrument_uid=? OR sector_uid=?)) + "
            "(SELECT count(*) FROM sector_context_mapping_version "
            "WHERE mapping_version_uid=? AND sector_uid=?)",
            (version_uid, entity_uid, entity_uid, version_uid, entity_uid),
        ),
        "CALENDAR": (
            "SELECT count(*) FROM trading_calendar_day WHERE exchange=? AND trading_date=?",
            (entity_uid, version_uid),
        ),
    }
    if kind not in queries:
        raise ValueError(f"unsupported reference entity kind: {kind}")
    sql, parameters = queries[kind]
    return int(connection.exec_driver_sql(sql, parameters).scalar_one()) == 1
