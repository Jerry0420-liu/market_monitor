from dataclasses import dataclass
from datetime import datetime, timedelta

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_data.models import HealthStatus, Watermark


@dataclass(frozen=True)
class HealthThresholds:
    minimum_coverage_ppm: int
    maximum_latency_ms: int
    valid_for_seconds: int

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_coverage_ppm <= 1_000_000:
            raise ValueError("minimum coverage must be between 0 and 1000000")
        if self.maximum_latency_ms < 0 or self.valid_for_seconds <= 0:
            raise ValueError("latency must be non-negative and validity positive")


class SourceEpochService:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def start_epoch(self, provider: str, fingerprint: str, started_at: datetime) -> str:
        uid = new_uid()
        instant = format_rfc3339(started_at)

        def command(transaction: TransactionContext) -> None:
            active = transaction.connection.exec_driver_sql(
                "SELECT epoch_uid,capability_fingerprint FROM market_source_epoch "
                "WHERE provider_key=? AND status='ACTIVE'",
                (provider,),
            ).one_or_none()
            if active is not None and active.capability_fingerprint == fingerprint:
                raise ValueError("identical active source epoch already exists")
            if active is not None:
                transaction.connection.exec_driver_sql(
                    "UPDATE capability_watermark SET rewarm_required=1,version=version+1 "
                    "WHERE epoch_uid=?",
                    (active.epoch_uid,),
                )
                transaction.connection.exec_driver_sql(
                    "UPDATE market_source_epoch SET status='RETIRED',ended_at=?,rewarm_required=1 "
                    "WHERE epoch_uid=?",
                    (instant, active.epoch_uid),
                )
            transaction.connection.exec_driver_sql(
                "INSERT INTO market_source_epoch"
                "(epoch_uid,provider_key,capability_fingerprint,started_at,ended_at,status,"
                "rewarm_required) VALUES (?,?,?,?,NULL,'ACTIVE',?)",
                (uid, provider, fingerprint, instant, int(active is not None)),
            )

        self._writer.submit(command).result()
        return uid


class CapabilityHealthService:
    def __init__(
        self, runtime: DatabaseRuntime, writer: WriterQueue, thresholds: HealthThresholds
    ) -> None:
        self._runtime = runtime
        self._writer = writer
        self._thresholds = thresholds

    def record(
        self,
        epoch_uid: str,
        capability: str,
        coverage_ppm: int,
        latency_ms: int,
        observed_at: datetime,
    ) -> HealthStatus:
        if not 0 <= coverage_ppm <= 1_000_000 or latency_ms < 0:
            raise ValueError("health measurements are outside valid range")
        severe = (
            coverage_ppm * 2 < self._thresholds.minimum_coverage_ppm
            or latency_ms > self._thresholds.maximum_latency_ms * 2
        )
        limited = (
            coverage_ppm < self._thresholds.minimum_coverage_ppm
            or latency_ms > self._thresholds.maximum_latency_ms
        )
        health = "UNHEALTHY" if severe else "DEGRADED" if limited else "HEALTHY"
        fitness = "UNFIT" if severe else "FIT_WITH_LIMITATIONS" if limited else "FIT"
        observed = format_rfc3339(observed_at)
        valid_until = format_rfc3339(
            observed_at + timedelta(seconds=self._thresholds.valid_for_seconds)
        )
        uid = new_uid()

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO capability_health_report"
                "(report_uid,epoch_uid,capability,health_status,fitness_status,coverage_ppm,"
                "latency_ms,observed_at,valid_until) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    epoch_uid,
                    capability,
                    health,
                    fitness,
                    coverage_ppm,
                    latency_ms,
                    observed,
                    valid_until,
                ),
            )

        self._writer.submit(command).result()
        return HealthStatus(health, fitness, valid_until)

    def effective_status(self, epoch_uid: str, capability: str, at: datetime) -> HealthStatus:
        instant = format_rfc3339(at)
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT health_status,fitness_status,valid_until FROM capability_health_report "
                "WHERE epoch_uid=? AND capability=? AND observed_at<=? "
                "ORDER BY observed_at DESC LIMIT 1",
                (epoch_uid, capability, instant),
            ).one_or_none()
        if row is None or str(row.valid_until) < instant:
            return HealthStatus("UNKNOWN", "UNKNOWN", instant)
        return HealthStatus(str(row.health_status), str(row.fitness_status), str(row.valid_until))


class WatermarkRepository:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def advance(
        self,
        epoch_uid: str,
        capability: str,
        event_time: datetime,
        received_time: datetime,
    ) -> Watermark:
        event = format_rfc3339(event_time)
        received = format_rfc3339(received_time)

        def command(transaction: TransactionContext) -> Watermark:
            row = transaction.connection.exec_driver_sql(
                "SELECT event_time,received_time,version,rewarm_required FROM capability_watermark "
                "WHERE epoch_uid=? AND capability=?",
                (epoch_uid, capability),
            ).one_or_none()
            if row is not None and str(row.event_time) >= event:
                return _watermark(row)
            if row is None:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO capability_watermark"
                    "(epoch_uid,capability,event_time,received_time,version,rewarm_required) "
                    "VALUES (?,?,?,?,1,0)",
                    (epoch_uid, capability, event, received),
                )
                return Watermark(event, received, 1, False)
            version = int(row.version) + 1
            transaction.connection.exec_driver_sql(
                "UPDATE capability_watermark SET event_time=?,received_time=?,version=? "
                "WHERE epoch_uid=? AND capability=?",
                (event, received, version, epoch_uid, capability),
            )
            return Watermark(event, received, version, bool(row.rewarm_required))

        return self._writer.submit(command).result()

    def get(self, epoch_uid: str, capability: str) -> Watermark:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT event_time,received_time,version,rewarm_required FROM capability_watermark "
                "WHERE epoch_uid=? AND capability=?",
                (epoch_uid, capability),
            ).one()
        return _watermark(row)


def _watermark(row: object) -> Watermark:
    return Watermark(
        getattr(row, "event_time"),
        str(getattr(row, "received_time")),
        int(getattr(row, "version")),
        bool(getattr(row, "rewarm_required")),
    )
