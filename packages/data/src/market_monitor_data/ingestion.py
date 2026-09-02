import json
from decimal import Decimal, InvalidOperation

from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import new_uid, sha256_bytes, to_scaled_integer
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_data.clock import TradingClock
from market_monitor_data.models import IngestionResult, ProviderBatch, QuoteVersion


class IngestionService:
    def __init__(
        self, runtime: DatabaseRuntime, writer: WriterQueue, artifacts: ArtifactStore
    ) -> None:
        self._runtime = runtime
        self._writer = writer
        self._artifacts = artifacts

    def ingest(self, epoch_uid: str, batch: ProviderBatch) -> IngestionResult:
        existing = self._existing(epoch_uid, batch.provider_batch_id)
        if existing is not None:
            return existing
        payload = json.dumps(
            {
                "id": batch.provider_batch_id,
                "received_at": batch.received_at,
                "records": [
                    {
                        "code": record.external_code,
                        "source_time": record.source_time,
                        "price": record.price,
                        "volume": record.volume,
                        "raw": record.raw,
                    }
                    for record in batch.records
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        artifact = self._artifacts.put_bytes(payload, "application/json")
        self._artifacts.register(artifact)
        batch_uid = new_uid()

        def command(transaction: TransactionContext) -> IngestionResult:
            provider = transaction.connection.exec_driver_sql(
                "SELECT provider_key FROM market_source_epoch WHERE epoch_uid=?",
                (epoch_uid,),
            ).scalar_one()
            lineages: list[str] = []
            quarantined = 0
            transaction.connection.exec_driver_sql(
                "INSERT INTO market_data_batch"
                "(batch_uid,epoch_uid,provider_batch_id,raw_artifact_sha256,received_at,status) "
                "VALUES (?,?,?,?,?,'RECEIVED')",
                (batch_uid, epoch_uid, batch.provider_batch_id, artifact.sha256, batch.received_at),
            )
            for index, record in enumerate(batch.records):
                mapping = transaction.connection.exec_driver_sql(
                    "SELECT mapping_status,instrument_uid FROM provider_mapping "
                    "WHERE provider_key=? AND external_code=? AND valid_from<=? "
                    "ORDER BY valid_from DESC LIMIT 1",
                    (provider, record.external_code, batch.received_at),
                ).one_or_none()
                status = "UNMAPPED" if mapping is None else str(mapping.mapping_status)
                transaction.connection.exec_driver_sql(
                    "INSERT INTO raw_market_record"
                    "(batch_uid,record_index,external_code,raw_source_time,payload_locator,"
                    "mapping_status) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        batch_uid,
                        index,
                        record.external_code,
                        record.source_time,
                        f"$.records[{index}]",
                        status,
                    ),
                )
                if mapping is None or status != "RESOLVED" or mapping.instrument_uid is None:
                    quarantined += 1
                    continue
                valid_time = TradingClock.validate_source_time(record.source_time)
                lineage_uid = new_uid()
                source_key = record.source_time or f"RECEIVED:{batch.received_at}"
                business_key = sha256_bytes(
                    f"{epoch_uid}|{mapping.instrument_uid}|{source_key}|LAST".encode()
                )
                existing_lineage = transaction.connection.exec_driver_sql(
                    "SELECT lineage_uid FROM quote_lineage WHERE business_key_sha256=?",
                    (business_key,),
                ).scalar_one_or_none()
                if existing_lineage is not None:
                    lineage_uid = str(existing_lineage)
                else:
                    transaction.connection.exec_driver_sql(
                        "INSERT INTO quote_lineage"
                        "(lineage_uid,epoch_uid,instrument_uid,source_time,quote_kind,"
                        "business_key_sha256) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            lineage_uid,
                            epoch_uid,
                            mapping.instrument_uid,
                            source_key,
                            "LAST",
                            business_key,
                        ),
                    )
                current = transaction.connection.exec_driver_sql(
                    "SELECT record_version FROM market_quote WHERE lineage_uid=? AND is_current=1",
                    (lineage_uid,),
                ).one_or_none()
                if current is not None:
                    lineages.append(lineage_uid)
                    continue
                price, price_status = _price(record.price)
                quote_uid = new_uid()
                transaction.connection.exec_driver_sql(
                    "INSERT INTO market_quote"
                    "(quote_uid,lineage_uid,record_version,batch_uid,record_index,received_at,"
                    "source_time_raw,source_time,price_scaled,price_scale,price_status,volume,"
                    "volume_status,is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (
                        quote_uid,
                        lineage_uid,
                        1,
                        batch_uid,
                        index,
                        batch.received_at,
                        record.source_time,
                        valid_time,
                        price,
                        4,
                        price_status,
                        record.volume,
                        "VALUE" if record.volume is not None else "MISSING",
                    ),
                )
                if record.source_time is not None and valid_time is None:
                    transaction.connection.exec_driver_sql(
                        "INSERT INTO quote_quality_issue(quote_uid,issue_code,detail) "
                        "VALUES (?,?,?)",
                        (quote_uid, "INVALID_SOURCE_TIME", record.source_time),
                    )
                lineages.append(lineage_uid)
            transaction.connection.exec_driver_sql(
                "UPDATE market_data_batch SET status=? WHERE batch_uid=?",
                ("NORMALIZED" if lineages else "QUARANTINED", batch_uid),
            )
            return IngestionResult(batch_uid, len(lineages), quarantined, tuple(lineages))

        return self._writer.submit(command).result()

    def correct_quote(
        self,
        lineage_uid: str,
        batch_uid: str,
        record_index: int,
        price: str | None,
        volume: int | None,
        received_at: str,
    ) -> str:
        quote_uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            current = transaction.connection.exec_driver_sql(
                "SELECT record_version,source_time_raw,source_time FROM market_quote "
                "WHERE lineage_uid=? AND is_current=1",
                (lineage_uid,),
            ).one()
            scaled, price_status = _price(price)
            transaction.connection.exec_driver_sql(
                "UPDATE market_quote SET is_current=0 WHERE lineage_uid=? AND is_current=1",
                (lineage_uid,),
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO market_quote"
                "(quote_uid,lineage_uid,record_version,batch_uid,record_index,received_at,"
                "source_time_raw,source_time,price_scaled,price_scale,price_status,volume,"
                "volume_status,is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    quote_uid,
                    lineage_uid,
                    int(current.record_version) + 1,
                    batch_uid,
                    record_index,
                    received_at,
                    current.source_time_raw,
                    current.source_time,
                    scaled,
                    4,
                    price_status,
                    volume,
                    "VALUE" if volume is not None else "MISSING",
                ),
            )

        self._writer.submit(command).result()
        return quote_uid

    def quote_history(self, lineage_uid: str) -> tuple[QuoteVersion, ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT quote_uid,record_version,source_time_raw,source_time,price_scaled,"
                "price_scale,volume,is_current FROM market_quote WHERE lineage_uid=? "
                "ORDER BY record_version",
                (lineage_uid,),
            ).all()
        return tuple(
            QuoteVersion(
                str(row.quote_uid),
                int(row.record_version),
                row.source_time_raw,
                row.source_time,
                row.price_scaled,
                int(row.price_scale),
                row.volume,
                bool(row.is_current),
            )
            for row in rows
        )

    def unreferenced_artifacts(self) -> tuple[str, ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT a.sha256 FROM artifact_object a LEFT JOIN market_data_batch b "
                "ON b.raw_artifact_sha256=a.sha256 WHERE b.batch_uid IS NULL "
                "AND a.media_type='application/json' ORDER BY a.sha256"
            ).scalars()
            return tuple(str(value) for value in rows)

    def _existing(self, epoch_uid: str, provider_batch_id: str) -> IngestionResult | None:
        with self._runtime.read_connection() as connection:
            batch_uid = connection.exec_driver_sql(
                "SELECT batch_uid FROM market_data_batch WHERE epoch_uid=? AND provider_batch_id=?",
                (epoch_uid, provider_batch_id),
            ).scalar_one_or_none()
            if batch_uid is None:
                return None
            lineages = tuple(
                str(value)
                for value in connection.exec_driver_sql(
                    "SELECT DISTINCT q.lineage_uid FROM market_quote q "
                    "WHERE q.batch_uid=? ORDER BY q.lineage_uid",
                    (batch_uid,),
                ).scalars()
            )
            quarantined = int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM raw_market_record WHERE batch_uid=? "
                    "AND mapping_status!='RESOLVED'",
                    (batch_uid,),
                ).scalar_one()
            )
        return IngestionResult(str(batch_uid), len(lineages), quarantined, lineages)


def _price(value: str | None) -> tuple[int | None, str]:
    if value is None:
        return None, "MISSING"
    try:
        return to_scaled_integer(Decimal(value), 4), "VALUE"
    except InvalidOperation, ValueError:
        return None, "INVALID"
