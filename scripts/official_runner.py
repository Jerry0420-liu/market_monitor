"""Fail-closed Native TDX OFFICIAL runner.

This process is deliberately separate from the Web/API process.  It composes the existing
provider, historical loader, snapshot builder, metric runner, Analysis Commit, and outbox
worker; it does not implement an alternative business path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for source in reversed(
    (
        ROOT / "packages" / "analysis" / "src",
        ROOT / "packages" / "contracts" / "src",
        ROOT / "packages" / "data" / "src",
        ROOT / "packages" / "notifications" / "src",
        ROOT / "packages" / "persistence" / "src",
    )
):
    sys.path.insert(0, str(source))

from market_monitor_analysis.analysis_commit import (  # noqa: E402
    AnalysisCommitRequest,
    AnalysisCommitService,
    MetricProvenance,
)
from market_monitor_analysis.metric_runner import MetricRunner  # noqa: E402
from market_monitor_analysis.official_gate import OfficialExecutionPermit  # noqa: E402
from market_monitor_analysis.official_orchestrator import (  # noqa: E402
    OfficialCommitInput,
    OfficialCommitResult,
    OfficialMetricOutput,
    OfficialOrchestrator,
    OfficialOrchestratorConfig,
    OfficialPipeline,
    SqliteCycleJournal,
    StageStatus,
)
from market_monitor_analysis.thresholds import (  # noqa: E402
    ThresholdRegistry,
    ThresholdUnavailableError,
)
from market_monitor_data.clock import TradingClock  # noqa: E402
from market_monitor_data.health import (  # noqa: E402
    CapabilityHealthService,
    HealthThresholds,
    SourceEpochService,
)
from market_monitor_data.ingestion import IngestionService  # noqa: E402
from market_monitor_data.reference import ReferenceRepository  # noqa: E402
from market_monitor_data.tdx.historical import TdxHistoricalLoader  # noqa: E402
from market_monitor_data.tdx.protocol import TdxProtocol  # noqa: E402
from market_monitor_data.tdx.provider import (  # noqa: E402
    NativeTdxProvider,
    TdxGateway,
    TdxMinuteIncrementResult,
    TdxUniverse,
)
from market_monitor_data.tdx.storage import TdxStorage  # noqa: E402
from market_monitor_data.tdx.transport import (  # noqa: E402
    TdxServer,
    TdxServerPool,
)
from market_monitor_notifications.adapters import (  # noqa: E402
    InAppAdapter,
    WebhookAdapter,
)
from market_monitor_notifications.worker import DeliveryWorker  # noqa: E402
from market_monitor_persistence.artifacts import ArtifactStore  # noqa: E402
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime  # noqa: E402
from market_monitor_persistence.migrations import MigrationManager  # noqa: E402
from market_monitor_persistence.values import format_rfc3339, new_uid  # noqa: E402
from market_monitor_persistence.writer import WriterQueue  # noqa: E402

from scripts.tdx_runner import (  # noqa: E402
    DEFAULT_SERVERS,
    _active_tdx_epoch,
    _import_listing_reference_facts,
    _import_trading_calendar,
    _prepare_metric_shadow_snapshots,
)

_REQUIRED_TDX_CAPABILITIES = (
    "SH_QUOTES",
    "SZ_QUOTES",
    "INDEX_QUOTES",
    "ETF_QUOTES",
    "MINUTE_BARS",
    "DAILY_BARS",
    "TDX_BLOCKS",
)
_PROVIDER_TDX_CAPABILITIES = tuple(
    value for value in _REQUIRED_TDX_CAPABILITIES if value != "MINUTE_BARS"
)
_SHADOW_GUARDIAN_THRESHOLD_VERSION = "guardian-thresholds-v1.0-prod"
_SHADOW_SCOUT_THRESHOLD_VERSION = "scout-thresholds-v1.0-prod"


@dataclass(frozen=True, slots=True)
class OfficialRuntimeSettings:
    """Environment boundary for the separate official worker."""

    data_directory: Path
    official_enabled: bool = False
    threshold_activation: int = 0
    subject_uid: str | None = None
    trading_calendar_file: Path | None = None
    listing_reference_file: Path | None = None
    servers: tuple[TdxServer, ...] = DEFAULT_SERVERS
    webhook_url: str | None = None
    webhook_enabled: bool = False

    def __post_init__(self) -> None:
        if self.threshold_activation < 0:
            raise ValueError("threshold_activation must not be negative")
        if not self.official_enabled and self.threshold_activation != 0:
            raise ValueError("disabled official runtime requires threshold_activation=0")
        if not self.servers:
            raise ValueError("official runtime requires at least one TDX server")
        if self.webhook_enabled and not self.webhook_url:
            raise ValueError("webhook cannot be enabled without an endpoint")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, data_directory: Path | None = None
    ) -> OfficialRuntimeSettings:
        values = os.environ if environ is None else environ
        raw_data = (
            str(data_directory)
            if data_directory is not None
            else values.get("MARKET_MONITOR_DATA_DIR", "")
        ).strip()
        if not raw_data:
            raise ValueError("MARKET_MONITOR_DATA_DIR is required")
        enabled = _boolean(values.get("MARKET_MONITOR_OFFICIAL_ENABLED", "false"))
        activation = _integer(values.get("MARKET_MONITOR_THRESHOLD_ACTIVATION", "0"))
        subject = values.get("MARKET_MONITOR_OFFICIAL_SUBJECT_UID", "").strip() or None
        calendar = _path_or_none(values.get("MARKET_MONITOR_TRADING_CALENDAR_FILE", ""))
        listing = _path_or_none(values.get("MARKET_MONITOR_LISTING_REFERENCE_FILE", ""))
        servers = _servers(values.get("MARKET_MONITOR_TDX_SERVERS", ""))
        webhook_url = values.get("MARKET_MONITOR_WEBHOOK_URL", "").strip() or None
        webhook_enabled = _boolean(values.get("MARKET_MONITOR_WEBHOOK_ENABLED", "false"))
        return cls(
            Path(raw_data),
            enabled,
            activation,
            subject,
            calendar,
            listing,
            servers,
            webhook_url,
            webhook_enabled,
        )


class NativeTdxOfficialPipeline(OfficialPipeline):
    """Compose the accepted Native TDX and analysis services for one subject cycle."""

    def __init__(
        self,
        runtime: DatabaseRuntime,
        writer: WriterQueue,
        artifacts: ArtifactStore,
        gateway: TdxGateway,
        *,
        subject_uid: str,
        threshold_activation: int,
        trading_calendar_file: Path | None = None,
        listing_reference_file: Path | None = None,
        minute_gateways: Sequence[TdxGateway] = (),
        webhook_url: str | None = None,
        webhook_enabled: bool = False,
        evaluation_disposition: Literal["OFFICIAL", "SHADOW"] = "OFFICIAL",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not subject_uid.strip():
            raise ValueError("official pipeline requires a subject UID")
        if evaluation_disposition not in {"OFFICIAL", "SHADOW"}:
            raise ValueError("evaluation disposition must be OFFICIAL or SHADOW")
        self._runtime = runtime
        self._writer = writer
        self._artifacts = artifacts
        self._gateway = gateway
        self._subject_uid = subject_uid
        self._threshold_activation = threshold_activation
        self._evaluation_disposition = evaluation_disposition
        self._calendar_file = trading_calendar_file
        self._listing_file = listing_reference_file
        self._now = now or (lambda: datetime.now(UTC))
        self._references = ReferenceRepository(runtime, writer)
        self._clock = TradingClock(runtime, writer)
        self._epoch_uid: str | None = None
        self._universe: TdxUniverse | None = None
        self._last_minute: TdxMinuteIncrementResult | None = None
        self._warmup: object | None = None
        self._listing_imported = False
        self._calendar_imported = False
        self._provider = NativeTdxProvider(
            runtime,
            self._references,
            IngestionService(runtime, writer, artifacts),
            artifacts,
            CapabilityHealthService(runtime, writer, HealthThresholds(900_000, 40_000, 60)),
            TdxStorage(runtime, writer),
            gateway,
            clock=self._clock,
            minute_gateways=minute_gateways or (gateway,),
        )
        self._historical = TdxHistoricalLoader(TdxStorage(runtime, writer), gateway, self._clock)
        self._metrics = MetricRunner(runtime, writer, artifacts)
        self._commits = AnalysisCommitService(runtime, writer)
        self._delivery = DeliveryWorker(
            runtime,
            writer,
            {
                "IN_APP": InAppAdapter(),
                "WEBHOOK": WebhookAdapter(webhook_url, enabled=webhook_enabled),
            },
            f"official-{new_uid()}",
            self._now,
        )

    def boot(self, observed_at: datetime) -> StageStatus:
        del observed_at
        try:
            revision = MigrationManager().verify(self._runtime)
            with self._runtime.read_connection() as connection:
                integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
            if integrity != "ok":
                raise RuntimeError("SQLite integrity check failed")
        except (OSError, RuntimeError, ValueError) as error:
            return StageStatus(
                False,
                "UNFIT",
                str(error),
                {"error_code": type(error).__name__},
            )
        return StageStatus(
            True,
            "FIT",
            data={"migration_revision": revision, "integrity": integrity},
        )

    def calendar_readiness(self, observed_at: datetime) -> StageStatus:
        try:
            if self._calendar_file is not None and not self._calendar_imported:
                _import_trading_calendar(self._calendar_file, self._runtime, self._writer)
                self._calendar_imported = True
            phases = {
                exchange: self._clock.phase_at(exchange, observed_at)
                for exchange in ("SSE", "SZSE")
            }
            if any(phase == "NON_TRADING_DAY" for phase in phases.values()):
                return StageStatus(
                    True,
                    "FIT",
                    data={"phases": phases, "calendar_state": "NON_TRADING_DAY"},
                )
            if len(set(phases.values())) != 1:
                return StageStatus(False, "UNFIT", data={"phases": phases})
        except (OSError, RuntimeError, ValueError) as error:
            return StageStatus(False, "UNFIT", str(error))
        return StageStatus(True, "FIT", data={"phases": phases})

    def reference_readiness(self, observed_at: datetime) -> StageStatus:
        try:
            if self._listing_file is not None and not self._listing_imported:
                _import_listing_reference_facts(
                    self._listing_file, self._references, self._artifacts, observed_at
                )
                self._listing_imported = True
            self._universe = self._provider.synchronize_reference(observed_at)
            if not self._universe.primary:
                return StageStatus(
                    False,
                    "UNFIT",
                    "no eligible Reference-backed Primary instruments",
                    {"primary_count": 0, "pre_listing_contamination": 0},
                )
            contamination = self._pre_listing_contamination(observed_at)
            if contamination:
                return StageStatus(
                    False,
                    "UNFIT",
                    "PRE_LISTING instruments reached Primary",
                    {"pre_listing_contamination": contamination},
                )
        except (OSError, RuntimeError, ValueError) as error:
            return StageStatus(False, "UNFIT", str(error))
        return StageStatus(
            True,
            "FIT",
            data={
                "primary_count": len(self._universe.primary),
                "context_index_count": len(self._universe.context_indexes),
                "context_etf_count": len(self._universe.context_etfs),
                "pre_listing_contamination": 0,
            },
        )

    def provider_readiness(self, observed_at: datetime) -> StageStatus:
        if self._universe is None:
            return StageStatus(False, "UNFIT", "Reference readiness has not completed")
        try:
            self._epoch_uid = _active_tdx_epoch(self._runtime)
            if self._epoch_uid is None:
                self._epoch_uid = SourceEpochService(self._runtime, self._writer).start_epoch(
                    NativeTdxProvider.PROVIDER_KEY, "cr003-native-tdx-v1", observed_at
                )
            poll = self._provider.poll_once(self._epoch_uid, observed_at)
            supporting = self._provider.refresh_supporting_data(
                self._epoch_uid, observed_at, onboard_official_sectors=True
            )
            states = {**supporting.health_capabilities, **poll.health_capabilities}
            return self._health_stage(
                states,
                {
                    "requested": poll.requested,
                    "returned": poll.returned,
                    "quarantined": poll.quarantined,
                    "duration_ms": poll.duration_ms,
                },
                _PROVIDER_TDX_CAPABILITIES,
            )
        except (OSError, RuntimeError, ValueError) as error:
            return StageStatus(False, "UNFIT", str(error))

    def historical_readiness(self, observed_at: datetime) -> StageStatus:
        if self._epoch_uid is None or self._universe is None:
            return StageStatus(False, "UNFIT", "provider readiness has not completed")
        try:
            warmup = self._historical.assess(self._epoch_uid, self._universe, observed_at)
            self._warmup = warmup
            if warmup.state != "FIT":
                return StageStatus(
                    False,
                    "FIT_WITH_LIMITATIONS",
                    "historical coverage is incomplete",
                    _warmup_data(warmup),
                )
            return StageStatus(True, "FIT", data=_warmup_data(warmup))
        except (OSError, RuntimeError, ValueError) as error:
            return StageStatus(False, "UNFIT", str(error))

    def minute_cohort_readiness(self, observed_at: datetime) -> StageStatus:
        if self._epoch_uid is None or self._universe is None:
            return StageStatus(False, "UNFIT", "provider readiness has not completed")
        try:
            targets = {
                market: self._clock.latest_completed_legal_minute(exchange, observed_at)
                for market, exchange in ((0, "SZSE"), (1, "SSE"))
            }
            if any(target is None for target in targets.values()):
                return StageStatus(False, "FIT", "no completed legal minute is available")
            self._last_minute = self._provider.refresh_primary_minute_bars(
                self._epoch_uid,
                {market: target for market, target in targets.items() if target is not None},
                observed_at,
            )
            ready = self._last_minute.health_capability == "HEALTHY"
            return StageStatus(
                ready,
                "FIT" if ready else "FIT_WITH_LIMITATIONS",
                data={
                    "coverage_ppm": self._last_minute.coverage_ppm,
                    "target_count": len(self._last_minute.targets),
                    "valid_latest_complete_count": self._last_minute.valid_latest_complete_count,
                    "expected_count": self._last_minute.expected_count,
                },
            )
        except (OSError, RuntimeError, ValueError) as error:
            return StageStatus(False, "UNFIT", str(error))

    def market_phase(self, observed_at: datetime) -> str:
        return self._clock.phase_at("SSE", observed_at)

    def threshold_readiness(self, observed_at: datetime) -> StageStatus:
        if self._evaluation_disposition == "SHADOW":
            try:
                registry = ThresholdRegistry(self._runtime, self._writer)
                guardian = registry.resolve_explicit(
                    "GUARDIAN", _SHADOW_GUARDIAN_THRESHOLD_VERSION, observed_at
                )
                scout = registry.resolve_explicit(
                    "SCOUT", _SHADOW_SCOUT_THRESHOLD_VERSION, observed_at
                )
            except ThresholdUnavailableError as error:
                return StageStatus(False, "FIT", str(error), {"activation_count": 0})
            return StageStatus(
                True,
                "FIT",
                "explicit threshold definitions resolved for SHADOW",
                {
                    "activation_count": 0,
                    "threshold_activation": 0,
                    "guardian_threshold_uid": guardian.uid,
                    "scout_threshold_uid": scout.uid,
                    "guardian_threshold_version": guardian.version,
                    "scout_threshold_version": scout.version,
                },
            )
        if self._threshold_activation == 0:
            return StageStatus(
                False,
                "FIT",
                "threshold activation is disabled",
                {"activation_count": 0, "threshold_activation": 0},
            )
        try:
            registry = ThresholdRegistry(self._runtime, self._writer)
            guardian = registry.resolve_official("GUARDIAN", observed_at)
            scout = registry.resolve_official("SCOUT", observed_at)
        except ThresholdUnavailableError as error:
            return StageStatus(
                False,
                "FIT",
                str(error),
                {"activation_count": 0, "threshold_activation": self._threshold_activation},
            )
        final_close = self._clock.final_continuous_close("SSE", observed_at)
        return StageStatus(
            True,
            "FIT",
            data={
                "activation_count": 2,
                "threshold_activation": self._threshold_activation,
                "guardian_threshold_uid": guardian.uid,
                "scout_threshold_uid": scout.uid,
                "guardian_threshold_version": guardian.version,
                "scout_threshold_version": scout.version,
                "final_close": final_close == observed_at,
            },
        )

    def seal_snapshot(self, subject_uid: str, cycle_key: str, observed_at: datetime) -> str:
        if self._epoch_uid is None or self._last_minute is None:
            raise RuntimeError("official snapshot inputs are not ready")
        snapshots = _prepare_metric_shadow_snapshots(
            self._runtime,
            self._writer,
            self._artifacts,
            self._epoch_uid,
            observed_at,
            cycle_key,
            minute_cohort=self._last_minute,
            disposition=self._evaluation_disposition,
            subject_uid=subject_uid,
        )
        if len(snapshots) != 1:
            raise RuntimeError("official snapshot preparation did not produce one subject snapshot")
        return snapshots[0]

    def run_metrics(
        self, snapshot_uid: str, permit: OfficialExecutionPermit | None
    ) -> OfficialMetricOutput:
        if permit is None:
            result = self._metrics.run(
                snapshot_uid,
                guardian_threshold_version=_SHADOW_GUARDIAN_THRESHOLD_VERSION,
                scout_threshold_version=_SHADOW_SCOUT_THRESHOLD_VERSION,
            )
        else:
            result = self._metrics.run_official(snapshot_uid, permit)
        available = result.quality.fitness_status != "UNFIT"
        return OfficialMetricOutput(
            result.producer_version,
            result.evidence_sha256,
            result.guardian_metrics,
            result.scout_metrics,
            "AVAILABLE" if available else "UNAVAILABLE",
            self._lifecycle_for(result.snapshot_uid) if available else None,
            result.quality.fitness_status,
        )

    def expected_projection_version(self, subject_uid: str) -> int:
        with self._runtime.read_connection() as connection:
            value = connection.exec_driver_sql(
                "SELECT version FROM current_state_projection WHERE subject_uid=?",
                (subject_uid,),
            ).scalar_one_or_none()
        return 0 if value is None else int(value)

    def commit(self, request: OfficialCommitInput) -> OfficialCommitResult:
        result = self._commits.commit(
            AnalysisCommitRequest(
                request.snapshot_uid,
                request.availability_state,
                request.lifecycle_state,
                request.expected_projection_version,
                request.guardian_metrics,
                request.scout_metrics,
                request.channels,
                MetricProvenance(request.producer_version, request.evidence_sha256),
            )
        )
        return OfficialCommitResult(
            result.commit_uid,
            result.event_version_uids,
            result.intent_uids,
        )

    def deliver(self) -> object:
        return self._delivery.run_once()

    def close(self) -> None:
        close = getattr(self._gateway, "close", None)
        if callable(close):
            close()

    def eligible_subject_uids(self) -> tuple[str, ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT subject_uid FROM analysis_subject WHERE subject_kind='SECTOR' "
                "ORDER BY subject_uid"
            ).scalars()
        return tuple(str(value) for value in rows)

    def _health_stage(
        self,
        states: Mapping[str, str],
        data: Mapping[str, object],
        required: Sequence[str],
    ) -> StageStatus:
        missing = tuple(
            capability for capability in required if states.get(capability) != "HEALTHY"
        )
        details = {**data, "capabilities": dict(states), "missing_or_unfit": missing}
        return StageStatus(not missing, "FIT" if not missing else "UNFIT", data=details)

    def _pre_listing_contamination(self, observed_at: datetime) -> int:
        if self._universe is None:
            return 0
        listings = self._references.provider_listing_states(
            NativeTdxProvider.PROVIDER_KEY, observed_at
        )
        return sum(
            listings.get(f"{instrument.market}:{instrument.code}") is None
            or listings[f"{instrument.market}:{instrument.code}"].listing_status == "PRE_LISTING"
            for instrument in self._universe.primary
        )

    def _lifecycle_for(self, snapshot_uid: str) -> str:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT p.effective_lifecycle_state,p.last_valid_lifecycle_state "
                "FROM current_state_projection p JOIN evaluation_snapshot s "
                "ON s.subject_uid=p.subject_uid WHERE s.snapshot_uid=?",
                (snapshot_uid,),
            ).one_or_none()
        if row is None:
            return "OBSERVING"
        return str(row.effective_lifecycle_state or row.last_valid_lifecycle_state or "OBSERVING")


def run_official_once(
    settings: OfficialRuntimeSettings,
    *,
    gateway: TdxGateway,
    minute_gateways: Sequence[TdxGateway] = (),
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Run one explicitly configured cycle and return a JSON-safe audit summary."""
    paths = DatabasePaths.from_data_directory(settings.data_directory)
    runtime = DatabaseRuntime.open(paths)
    writer = WriterQueue(runtime)
    try:
        MigrationManager().upgrade(runtime)
        if not settings.official_enabled:
            return {
                "ok": True,
                "ready_for_official": False,
                "official_gate": "CLOSED",
                "threshold_activation": 0,
                "side_effects": "DISABLED",
            }
        if settings.threshold_activation == 0:
            return {
                "ok": True,
                "ready_for_official": False,
                "official_gate": "CLOSED",
                "threshold_activation": 0,
                "reason": "THRESHOLD_ACTIVATION_UNAVAILABLE",
            }
        if settings.subject_uid is None:
            raise ValueError("MARKET_MONITOR_OFFICIAL_SUBJECT_UID is required when enabled")
        writer.start()
        artifacts = ArtifactStore(runtime, writer)
        pipeline = NativeTdxOfficialPipeline(
            runtime,
            writer,
            artifacts,
            gateway,
            subject_uid=settings.subject_uid,
            threshold_activation=settings.threshold_activation,
            trading_calendar_file=settings.trading_calendar_file,
            listing_reference_file=settings.listing_reference_file,
            minute_gateways=minute_gateways,
            webhook_url=settings.webhook_url,
            webhook_enabled=settings.webhook_enabled,
            now=now,
        )
        observed_at = (now or (lambda: datetime.now(UTC)))()
        orchestrator = OfficialOrchestrator(
            pipeline,
            SqliteCycleJournal(runtime, writer),
            OfficialOrchestratorConfig(official_enabled=True),
        )
        try:
            result = orchestrator.run(
                settings.subject_uid, f"official-{format_rfc3339(observed_at)}", observed_at
            )
        finally:
            orchestrator.close()
        return {
            "ok": True,
            "ready_for_official": result.status.value == "COMMITTED",
            "status": result.status.value,
            "reason": result.reason,
            "cycle_key": result.cycle_key,
            "subject_uid": result.subject_uid,
            "phase": result.phase,
            "details": dict(result.details),
        }
    finally:
        writer.close()
        runtime.close()


def _warmup_data(value: object) -> dict[str, object]:
    return {
        name: getattr(value, name)
        for name in (
            "state",
            "coverage_ppm",
            "coverage_checked",
            "symbols_skipped",
            "fetch_requests",
            "fetched_bars",
            "inserted_bars",
            "duplicate_bars",
            "missing_symbols",
            "catchup_duration_ms",
        )
        if hasattr(value, name)
    }


def _path_or_none(value: str) -> Path | None:
    return Path(value.strip()) if value.strip() else None


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ValueError("boolean environment value is invalid")


def _integer(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError("integer environment value is invalid") from None


def _servers(value: str) -> tuple[TdxServer, ...]:
    if not value.strip():
        return DEFAULT_SERVERS
    result: list[TdxServer] = []
    for item in value.split(","):
        host, separator, port = item.strip().rpartition(":")
        if not separator or not host or not port.isdigit():
            raise ValueError("MARKET_MONITOR_TDX_SERVERS must contain host:port values")
        result.append(TdxServer(host, int(port)))
    return tuple(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the disabled-by-default Native TDX official cycle"
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--server", action="append", default=[])
    parser.add_argument("--subject-uid")
    parser.add_argument("--trading-calendar-file", type=Path)
    parser.add_argument("--listing-reference-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        settings = OfficialRuntimeSettings.from_environment(
            {
                **os.environ,
                **(
                    {"MARKET_MONITOR_OFFICIAL_SUBJECT_UID": arguments.subject_uid}
                    if arguments.subject_uid
                    else {}
                ),
                **(
                    {"MARKET_MONITOR_TRADING_CALENDAR_FILE": str(arguments.trading_calendar_file)}
                    if arguments.trading_calendar_file
                    else {}
                ),
                **(
                    {"MARKET_MONITOR_LISTING_REFERENCE_FILE": str(arguments.listing_reference_file)}
                    if arguments.listing_reference_file
                    else {}
                ),
                **(
                    {"MARKET_MONITOR_TDX_SERVERS": ",".join(arguments.server)}
                    if arguments.server
                    else {}
                ),
            },
            data_directory=arguments.data_dir,
        )
        if not settings.official_enabled or settings.threshold_activation == 0:
            report = run_official_once(settings, gateway=cast(TdxGateway, _DisabledGateway()))
        else:
            protocol = _protocol()
            pools = tuple(TdxServerPool(settings.servers, protocol) for _ in range(5))
            try:
                from market_monitor_data.tdx.client import TdxLiveClient

                gateways = tuple(TdxLiveClient(pool, protocol) for pool in pools)
                report = run_official_once(
                    settings,
                    gateway=gateways[0],
                    minute_gateways=gateways[1:],
                )
            finally:
                for pool in pools:
                    pool.close()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 1


class _DisabledGateway:
    def security_directory(self) -> tuple[object, ...]:
        raise RuntimeError("TDX gateway is disabled")


def _protocol() -> TdxProtocol:
    return TdxProtocol()


if __name__ == "__main__":
    raise SystemExit(main())
