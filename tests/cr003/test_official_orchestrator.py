from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from market_monitor_analysis.official_gate import OfficialExecutionPermit
from market_monitor_analysis.official_orchestrator import (
    CycleStatus,
    InMemoryCycleJournal,
    OfficialCommitInput,
    OfficialCommitResult,
    OfficialMetricOutput,
    OfficialOrchestrator,
    OfficialOrchestratorConfig,
    StageStatus,
)

NOW = datetime(2026, 8, 26, 2, 0, tzinfo=UTC)


def test_shadow_mode_runs_the_existing_pipeline_without_official_side_effects() -> None:
    pipeline = FakePipeline(threshold_status=StageStatus(True, data={"activation_count": 0}))
    journal = InMemoryCycleJournal()
    orchestrator = OfficialOrchestrator(
        pipeline,
        journal,
        OfficialOrchestratorConfig(evaluation_disposition="SHADOW"),
    )

    result = orchestrator.run("subject-1", "shadow-cycle-1", NOW)

    assert result.status is CycleStatus.SHADOW_COMPLETED
    assert result.details["evaluation_disposition"] == "SHADOW"
    assert result.details["snapshot_uid"] == "snapshot-1"
    assert pipeline.calls == [
        "boot",
        "calendar",
        "reference",
        "provider",
        "historical",
        "minute",
        "phase",
        "threshold",
        "snapshot",
        "metrics",
    ]
    assert pipeline.permits == [None]
    assert pipeline.commits == []
    assert pipeline.delivery_count == 0
    assert journal.records == {}


@dataclass
class FakePipeline:
    phase: str = "CONTINUOUS_AM"
    boot_status: StageStatus = field(default_factory=lambda: StageStatus(True))
    calendar_status: StageStatus = field(default_factory=lambda: StageStatus(True))
    reference_status: StageStatus = field(default_factory=lambda: StageStatus(True))
    provider_status: StageStatus = field(default_factory=lambda: StageStatus(True))
    historical_status: StageStatus = field(default_factory=lambda: StageStatus(True))
    minute_status: StageStatus = field(default_factory=lambda: StageStatus(True))
    threshold_status: StageStatus = field(
        default_factory=lambda: StageStatus(
            True,
            data={
                "activation_count": 2,
                "guardian_threshold_uid": "guardian-threshold-uid",
                "scout_threshold_uid": "scout-threshold-uid",
            },
        )
    )
    metric_output: OfficialMetricOutput = field(
        default_factory=lambda: OfficialMetricOutput(
            producer_version="cr004-market-metrics-v1",
            evidence_sha256="e" * 64,
            guardian_metrics={"RISE_RATE_PPM": 0},
            scout_metrics={"EARLY_ACTIVITY_PPM": 0},
            availability_state="AVAILABLE",
            lifecycle_state="OBSERVING",
            fitness_status="FIT",
        )
    )
    calls: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    permits: list[OfficialExecutionPermit | None] = field(default_factory=list)
    commits: list[OfficialCommitInput] = field(default_factory=list)
    commit_count: int = 0
    delivery_count: int = 0
    close_count: int = 0
    fail_commit_once: bool = False
    committed_by_snapshot: dict[str, OfficialCommitResult] = field(default_factory=dict)

    def boot(self, observed_at: datetime) -> StageStatus:
        self.calls.append("boot")
        return self.boot_status

    def calendar_readiness(self, observed_at: datetime) -> StageStatus:
        self.calls.append("calendar")
        return self.calendar_status

    def reference_readiness(self, observed_at: datetime) -> StageStatus:
        self.calls.append("reference")
        return self.reference_status

    def provider_readiness(self, observed_at: datetime) -> StageStatus:
        self.calls.append("provider")
        return self.provider_status

    def historical_readiness(self, observed_at: datetime) -> StageStatus:
        self.calls.append("historical")
        return self.historical_status

    def minute_cohort_readiness(self, observed_at: datetime) -> StageStatus:
        self.calls.append("minute")
        return self.minute_status

    def market_phase(self, observed_at: datetime) -> str:
        self.calls.append("phase")
        return self.phase

    def threshold_readiness(self, observed_at: datetime) -> StageStatus:
        self.calls.append("threshold")
        return self.threshold_status

    def seal_snapshot(self, subject_uid: str, cycle_key: str, observed_at: datetime) -> str:
        self.calls.append("snapshot")
        snapshot_uid = f"snapshot-{len(self.snapshots) + 1}"
        self.snapshots.append(snapshot_uid)
        return snapshot_uid

    def run_metrics(
        self, snapshot_uid: str, permit: OfficialExecutionPermit | None
    ) -> OfficialMetricOutput:
        self.calls.append("metrics")
        self.permits.append(permit)
        return self.metric_output

    def expected_projection_version(self, subject_uid: str) -> int:
        self.calls.append("projection")
        return 0

    def commit(self, request: OfficialCommitInput) -> OfficialCommitResult:
        self.calls.append("commit")
        self.commits.append(request)
        if request.snapshot_uid in self.committed_by_snapshot:
            return self.committed_by_snapshot[request.snapshot_uid]
        result = OfficialCommitResult(
            commit_uid=f"commit-{self.commit_count + 1}",
            event_version_uids=(f"event-version-{self.commit_count + 1}",),
            intent_uids=(f"intent-{self.commit_count + 1}",),
        )
        self.commit_count += 1
        self.committed_by_snapshot[request.snapshot_uid] = result
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise RuntimeError("crash after commit")
        return result

    def deliver(self) -> object:
        self.calls.append("deliver")
        self.delivery_count += 1
        return {"delivered": True}

    def close(self) -> None:
        self.close_count += 1


def _orchestrator(
    pipeline: FakePipeline, *, enabled: bool = True
) -> tuple[OfficialOrchestrator, InMemoryCycleJournal]:
    journal = InMemoryCycleJournal()
    return (
        OfficialOrchestrator(
            pipeline,
            journal,
            OfficialOrchestratorConfig(official_enabled=enabled),
        ),
        journal,
    )


def test_official_disabled_has_no_business_side_effects() -> None:
    pipeline = FakePipeline()
    orchestrator, journal = _orchestrator(pipeline, enabled=False)

    result = orchestrator.run("subject-1", "cycle-disabled", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == "OFFICIAL_DISABLED"
    assert pipeline.calls == ["boot"]
    assert journal.records == {}
    assert pipeline.commit_count == 0


def test_zero_threshold_activation_fails_closed() -> None:
    pipeline = FakePipeline(threshold_status=StageStatus(True, data={"activation_count": 0}))
    orchestrator, journal = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-zero-threshold", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == "THRESHOLD_ACTIVATION_UNAVAILABLE"
    assert "snapshot" not in pipeline.calls
    assert journal.records == {}


def test_valid_isolated_active_thresholds_produce_exactly_one_commit() -> None:
    pipeline = FakePipeline()
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-one", NOW)

    assert result.status is CycleStatus.COMMITTED
    assert pipeline.commit_count == 1
    assert pipeline.calls.index("snapshot") < pipeline.calls.index("metrics")
    assert pipeline.calls.index("metrics") < pipeline.calls.index("commit")
    assert pipeline.calls.index("commit") < pipeline.calls.index("deliver")


def test_permit_is_issued_after_snapshot_and_bound_to_exact_lineage() -> None:
    pipeline = FakePipeline()
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-lineage", NOW)

    assert result.status is CycleStatus.COMMITTED
    assert len(pipeline.permits) == 1
    permit = pipeline.permits[0]
    assert permit is not None
    assert permit.subject_uid == "subject-1"
    assert permit.snapshot_uid == pipeline.snapshots[0]
    assert permit.cycle_key == "cycle-lineage"
    assert pipeline.calls.index("snapshot") < pipeline.calls.index("metrics")
    assert not hasattr(OfficialExecutionPermit, "issue")


def test_duplicate_cycle_is_idempotent() -> None:
    pipeline = FakePipeline()
    orchestrator, _ = _orchestrator(pipeline)

    first = orchestrator.run("subject-1", "cycle-duplicate", NOW)
    calls_after_first = list(pipeline.calls)
    second = orchestrator.run("subject-1", "cycle-duplicate", NOW)

    assert first.status is CycleStatus.COMMITTED
    assert second.status is CycleStatus.IDEMPOTENT
    assert pipeline.commit_count == 1
    assert pipeline.calls == calls_after_first


def test_restart_does_not_duplicate_event_or_notification() -> None:
    pipeline = FakePipeline()
    journal = InMemoryCycleJournal()
    first = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    ).run("subject-1", "cycle-restart", NOW)
    restarted = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    ).run("subject-1", "cycle-restart", NOW)

    assert first.status is CycleStatus.COMMITTED
    assert restarted.status is CycleStatus.IDEMPOTENT
    assert pipeline.commit_count == 1
    assert pipeline.delivery_count == 1


def test_crash_before_commit_resumes_from_sealed_snapshot() -> None:
    pipeline = FakePipeline()
    pipeline.metric_output = OfficialMetricOutput(
        producer_version="cr004-market-metrics-v1",
        evidence_sha256="f" * 64,
        guardian_metrics={"RISE_RATE_PPM": 0},
        scout_metrics={},
        availability_state="AVAILABLE",
        lifecycle_state="OBSERVING",
        fitness_status="FIT",
    )
    journal = InMemoryCycleJournal()
    first = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    )
    pipeline.metric_output = pipeline.metric_output
    original_run_metrics = pipeline.run_metrics
    failed = {"once": True}

    def fail_once(
        snapshot_uid: str, permit: OfficialExecutionPermit | None
    ) -> OfficialMetricOutput:
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("crash before commit")
        return original_run_metrics(snapshot_uid, permit)

    pipeline.run_metrics = fail_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before commit"):
        first.run("subject-1", "cycle-before-commit", NOW)
    assert len(pipeline.snapshots) == 1

    pipeline.run_metrics = original_run_metrics  # type: ignore[method-assign]
    result = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    ).run("subject-1", "cycle-before-commit", NOW)

    assert result.status is CycleStatus.COMMITTED
    assert len(pipeline.snapshots) == 1
    assert pipeline.commit_count == 1


def test_crash_after_commit_retries_without_duplicate_commit() -> None:
    pipeline = FakePipeline(fail_commit_once=True)
    journal = InMemoryCycleJournal()
    orchestrator = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    )

    with pytest.raises(RuntimeError, match="crash after commit"):
        orchestrator.run("subject-1", "cycle-after-commit", NOW)
    result = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    ).run("subject-1", "cycle-after-commit", NOW)

    assert result.status is CycleStatus.COMMITTED
    assert pipeline.commit_count == 1
    assert len(pipeline.commits) == 2


def test_fit_with_limitations_reaches_guardian_protected_commit() -> None:
    pipeline = FakePipeline(
        provider_status=StageStatus(True, fitness_status="FIT_WITH_LIMITATIONS"),
        metric_output=OfficialMetricOutput(
            producer_version="cr004-market-metrics-v1",
            evidence_sha256="a" * 64,
            guardian_metrics={"RISE_RATE_PPM": 0},
            scout_metrics={"EARLY_ACTIVITY_PPM": 900_000},
            availability_state="AVAILABLE",
            lifecycle_state="OBSERVING",
            fitness_status="FIT_WITH_LIMITATIONS",
        ),
    )
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-limited", NOW)

    assert result.status is CycleStatus.COMMITTED
    assert pipeline.commits[0].availability_state == "AVAILABLE"
    assert pipeline.commits[0].fitness_status == "FIT_WITH_LIMITATIONS"


def test_unfit_provider_has_no_positive_scout_or_commit() -> None:
    pipeline = FakePipeline(provider_status=StageStatus(False, fitness_status="UNFIT"))
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-unfit", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == "PROVIDER_UNFIT"
    assert "metrics" not in pipeline.calls
    assert pipeline.commit_count == 0


@pytest.mark.parametrize("fitness", ["UNFIT", "UNKNOWN"])
def test_ready_stage_with_unsafe_fitness_fails_closed(fitness: str) -> None:
    pipeline = FakePipeline(provider_status=StageStatus(True, fitness_status=fitness))
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", f"cycle-{fitness.lower()}", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == f"PROVIDER_{fitness}"
    assert "snapshot" not in pipeline.calls
    assert pipeline.commit_count == 0


def test_stale_provider_fails_closed_before_snapshot() -> None:
    pipeline = FakePipeline(provider_status=StageStatus(False, detail="STALE_PROVIDER"))
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-stale", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == "PROVIDER_NOT_READY"
    assert "snapshot" not in pipeline.calls


@pytest.mark.parametrize("phase", ["BREAK", "NON_TRADING_DAY", "PRE_OPEN"])
def test_lunch_and_non_trading_phases_do_not_create_intraday_official(phase: str) -> None:
    pipeline = FakePipeline(phase=phase)
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", f"cycle-{phase}", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason in {"MARKET_BREAK", "NON_TRADING_DAY", "MARKET_NOT_OPEN"}
    assert "snapshot" not in pipeline.calls


def test_afternoon_resume_is_ready() -> None:
    pipeline = FakePipeline(phase="CONTINUOUS_PM")
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-afternoon", NOW)

    assert result.status is CycleStatus.COMMITTED


def test_close_allows_one_final_evaluation_and_rejects_duplicate_key() -> None:
    pipeline = FakePipeline(phase="CLOSED")
    pipeline.threshold_status = StageStatus(
        True,
        data={
            "activation_count": 2,
            "guardian_threshold_uid": "guardian-threshold-uid",
            "scout_threshold_uid": "scout-threshold-uid",
            "final_close": True,
        },
    )
    journal = InMemoryCycleJournal()
    orchestrator = OfficialOrchestrator(
        pipeline, journal, OfficialOrchestratorConfig(official_enabled=True)
    )

    first = orchestrator.run("subject-1", "final-close-2026-08-26", NOW)
    second = orchestrator.run("subject-1", "final-close-2026-08-26", NOW)

    assert first.status is CycleStatus.COMMITTED
    assert second.status is CycleStatus.IDEMPOTENT
    assert pipeline.commit_count == 1


def test_close_without_final_close_marker_fails_closed() -> None:
    pipeline = FakePipeline(phase="CLOSED")
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-close-no-marker", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == "FINAL_EVALUATION_NOT_PERMITTED"


def test_graceful_shutdown_closes_pipeline_once() -> None:
    pipeline = FakePipeline()
    orchestrator, _ = _orchestrator(pipeline)

    orchestrator.close()
    orchestrator.close()

    assert pipeline.close_count == 1


def test_provider_degradation_is_exposed_and_unfit_is_blocked() -> None:
    pipeline = FakePipeline(
        provider_status=StageStatus(
            False, fitness_status="UNFIT", data={"failover_count": 1, "active_nodes": 0}
        )
    )
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-failover", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.details["failover_count"] == 1


def test_official_write_path_is_only_commit_callback() -> None:
    pipeline = FakePipeline()
    orchestrator, _ = _orchestrator(pipeline)

    orchestrator.run("subject-1", "cycle-write-path", NOW)

    assert pipeline.calls.count("commit") == 1
    assert all(call not in pipeline.calls for call in ("direct_event", "direct_notification"))


def test_threshold_gate_data_is_carried_into_block_reason() -> None:
    pipeline = FakePipeline(
        threshold_status=StageStatus(
            False, detail="VALIDATION_MISSING", data={"activation_count": 0}
        )
    )
    orchestrator, _ = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-threshold-missing", NOW)

    assert result.status is CycleStatus.BLOCKED
    assert result.reason == "THRESHOLD_ACTIVATION_UNAVAILABLE"
    assert result.details["activation_count"] == 0


def test_readiness_stage_order_is_stable() -> None:
    pipeline = FakePipeline()
    orchestrator, _ = _orchestrator(pipeline)

    orchestrator.run("subject-1", "cycle-order", NOW)

    assert pipeline.calls[:8] == [
        "boot",
        "calendar",
        "reference",
        "provider",
        "historical",
        "minute",
        "phase",
        "threshold",
    ]


def test_cycle_journal_accumulates_threshold_metric_and_commit_lineage() -> None:
    pipeline = FakePipeline()
    orchestrator, journal = _orchestrator(pipeline)

    result = orchestrator.run("subject-1", "cycle-details", NOW)

    checkpoint = journal.get("cycle-details", "subject-1")
    assert result.status is CycleStatus.COMMITTED
    assert checkpoint is not None
    assert checkpoint.details == {
        "threshold_activation_count": 2,
        "guardian_threshold_uid": "guardian-threshold-uid",
        "scout_threshold_uid": "scout-threshold-uid",
        "fitness_status": "FIT",
        "metric_producer_version": "cr004-market-metrics-v1",
        "metric_evidence_sha256": "e" * 64,
        "event_version_count": 1,
        "intent_count": 1,
        "event_version_uids": ("event-version-1",),
        "intent_uids": ("intent-1",),
    }
