from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

from market_monitor_analysis.production_worker import ContinuousProductionWorker
from scripts.production_worker import _bar_retention_cutoff

CHINA = timezone(timedelta(hours=8))


class RetentionClock:
    def previous_valid_trading_dates(
        self, exchange: str, before: str, count: int
    ) -> tuple[str, ...]:
        assert exchange == "SSE"
        assert before == "2026-09-07"
        assert count == 5
        return (
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
            "2026-09-05",
        )


def test_bar_retention_cutoff_keeps_current_and_previous_five_trading_days() -> None:
    cutoff = _bar_retention_cutoff(RetentionClock(), datetime(2026, 9, 7, 10, tzinfo=CHINA), 5)

    assert cutoff == datetime(2026, 9, 1, tzinfo=CHINA).astimezone(timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.phase = "CONTINUOUS_AM"

    def phase_at(self, exchange: str, instant: datetime) -> str:
        del exchange, instant
        return self.phase

    def latest_completed_continuous_minute(
        self, exchange: str, instant: datetime
    ) -> datetime | None:
        del exchange
        minute = instant.replace(second=0, microsecond=0)
        if minute.hour == 9 and minute.minute == 30:
            return None
        return minute

    def latest_completed_legal_minute(self, exchange: str, instant: datetime) -> datetime | None:
        del exchange
        minute = instant.replace(second=0, microsecond=0)
        return minute if (minute.hour, minute.minute) in {(11, 30), (15, 0)} else None


def test_worker_uses_only_completed_minutes_and_deduplicates_ticks() -> None:
    clock = FakeClock()
    calls: list[tuple[str, datetime]] = []
    worker = ContinuousProductionWorker(clock, lambda key, at: calls.append((key, at)))

    assert worker.tick(datetime(2026, 9, 7, 9, 30, tzinfo=CHINA)).status == "NO_CYCLE"
    first = worker.tick(datetime(2026, 9, 7, 9, 31, tzinfo=CHINA))
    duplicate = worker.tick(datetime(2026, 9, 7, 9, 31, 30, tzinfo=CHINA))
    assert first.status == "COMPLETED"
    assert duplicate.status == "SKIPPED_DUPLICATE"
    assert [key for key, _ in calls] == ["2026-09-07:09:31"]

    clock.phase = "BREAK"
    assert worker.tick(datetime(2026, 9, 7, 11, 30, tzinfo=CHINA)).status == "COMPLETED"
    assert worker.tick(datetime(2026, 9, 7, 12, 30, tzinfo=CHINA)).status == "NO_CYCLE"

    clock.phase = "CONTINUOUS_PM"
    assert worker.tick(datetime(2026, 9, 7, 13, 1, tzinfo=CHINA)).status == "COMPLETED"
    clock.phase = "CLOSED"
    assert worker.tick(datetime(2026, 9, 7, 15, 0, tzinfo=CHINA)).status == "COMPLETED"
    assert worker.tick(datetime(2026, 9, 7, 15, 1, tzinfo=CHINA)).status == "NO_CYCLE"


def test_worker_isolates_one_cycle_failure_and_continues() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def run_cycle(key: str, _: datetime) -> None:
        calls.append(key)
        if len(calls) == 1:
            raise RuntimeError("one cycle failed")

    worker = ContinuousProductionWorker(clock, run_cycle)
    assert worker.tick(datetime(2026, 9, 7, 9, 31, tzinfo=CHINA)).status == "FAILED"
    assert worker.tick(datetime(2026, 9, 7, 9, 32, tzinfo=CHINA)).status == "COMPLETED"
    assert calls == ["2026-09-07:09:31", "2026-09-07:09:32"]


def test_worker_does_not_overlap_active_cycles() -> None:
    clock = FakeClock()
    entered = Event()
    release = Event()
    active = 0
    max_active = 0
    state_lock = Lock()

    def run_cycle(_: str, __: datetime) -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(2)
        with state_lock:
            active -= 1

    worker = ContinuousProductionWorker(clock, run_cycle)
    first = Thread(target=worker.tick, args=(datetime(2026, 9, 7, 9, 31, tzinfo=CHINA),))
    first.start()
    assert entered.wait(1)
    busy = worker.tick(datetime(2026, 9, 7, 9, 32, tzinfo=CHINA))
    assert busy.status == "SKIPPED_BUSY"
    release.set()
    first.join(2)
    assert max_active == 1


def test_worker_shutdown_waits_for_current_cycle_and_does_not_start_another() -> None:
    clock = FakeClock()
    entered = Event()
    release = Event()
    calls: list[str] = []

    def run_cycle(key: str, _: datetime) -> None:
        calls.append(key)
        entered.set()
        release.wait(2)

    now = datetime(2026, 9, 7, 9, 31, tzinfo=CHINA)
    worker = ContinuousProductionWorker(clock, run_cycle, poll_seconds=0.05, now=lambda: now)
    worker.start()
    assert entered.wait(1)
    worker.close()
    assert worker.is_running is False
    assert calls == ["2026-09-07:09:31"]
    release.set()


def test_restart_starts_at_current_target_without_catch_up() -> None:
    clock = FakeClock()
    calls: list[str] = []
    worker = ContinuousProductionWorker(clock, lambda key, _: calls.append(key))
    assert worker.tick(datetime(2026, 9, 7, 9, 35, tzinfo=CHINA)).status == "COMPLETED"
    restarted = ContinuousProductionWorker(clock, lambda key, _: calls.append(key))
    assert restarted.tick(datetime(2026, 9, 7, 9, 37, tzinfo=CHINA)).status == "COMPLETED"
    assert calls == ["2026-09-07:09:35", "2026-09-07:09:37"]
