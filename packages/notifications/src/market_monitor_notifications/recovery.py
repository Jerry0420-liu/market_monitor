from dataclasses import dataclass
from datetime import datetime

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue


@dataclass(frozen=True)
class RestoreSuppressionResult:
    generation: int
    cancelled: int


def advance_restore_generation(
    runtime: DatabaseRuntime, writer: WriterQueue, occurred_at: datetime
) -> RestoreSuppressionResult:
    _ = runtime
    timestamp = format_rfc3339(occurred_at)

    def command(transaction: TransactionContext) -> RestoreSuppressionResult:
        connection = transaction.connection
        row = connection.exec_driver_sql(
            "SELECT value,version FROM system_metadata WHERE key='restore_generation'"
        ).one()
        generation = int(row.value) + 1
        connection.exec_driver_sql(
            "UPDATE system_metadata SET value=?,updated_at=?,version=version+1 "
            "WHERE key='restore_generation' AND version=?",
            (str(generation), timestamp, row.version),
        )
        cancelled = connection.exec_driver_sql(
            "UPDATE notification_delivery_state SET delivery_status='CANCELLED',"
            "next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,"
            "last_error_code='RESTORE_GENERATION_SUPPRESSED',version=version+1,updated_at=? "
            "WHERE delivery_status IN ('PENDING','PROCESSING','RETRY_WAIT')",
            (timestamp,),
        ).rowcount
        detail_hash = canonical_hash({"restore_generation": generation, "cancelled": cancelled})
        connection.exec_driver_sql(
            "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
            "detail_hash,created_at) VALUES (?,'RESTORE_GENERATION_ADVANCED',NULL,NULL,?,?)",
            (new_uid(), detail_hash, timestamp),
        )
        return RestoreSuppressionResult(generation, int(cancelled))

    return writer.submit(command).result()
