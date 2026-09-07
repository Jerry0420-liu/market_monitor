"""Continuous market-minute orchestration around the existing production cycle."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from market_monitor_persistence.values import format_rfc3339

_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


class WorkerClock(Protocol):
    def phase_at(self, exchange: str, instant: datetime) -> str: ...

    def latest_completed_continuous_minute(
        self, exchange: str, instant: datetime
    ) -> datetime | None: ...

    def latest_completed_legal_minute(
        self, exchange: str, instant: datetime
    ) -> datetime | None: ...


CycleRunner = Callable[[str, datetime], Any]


@dataclass(frozen=True, slots=True)
class WorkerTick:
    status: str
    cycle_key: str | None = None
    target_minute: datetime | None = None
    result: Any = None
    reason: str | None = None


class ContinuousProductionWorker:
    """Run one existing production cycle per completed legal market minute."""

    _CONTINUOUS_PHASES = frozenset({"CONTINUOUS_AM", "CONTINUOUS_PM"})

    def __init__(
        self,
        clock: WorkerClock,
        run_cycle: CycleRunner | None = None,
        *,
        poll_seconds: float = 1.0,
        close_grace_seconds: float = 2.0,
        now: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if poll_seconds < 0.05 or poll_seconds > 60:
            raise ValueError("worker poll interval is outside the accepted bounds")
        if close_grace_seconds < 0 or close_grace_seconds > 10:
            raise ValueError("worker close grace is outside the accepted bounds")
        self._clock = clock
        self._run_cycle = run_cycle
        self._poll_seconds = poll_seconds
        self._close_grace = timedelta(seconds=close_grace_seconds)
        self._now = now or (lambda: datetime.now().astimezone())
        self._logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_cycle_key: str | None = None
        self._last_session_state: tuple[str, str] | None = None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def last_cycle_key(self) -> str | None:
        with self._state_lock:
            return self._last_cycle_key

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self.run_forever,
                name="market-monitor-production-worker",
                daemon=False,
            )
            self._thread.start()

    def close(self) -> None:
        with self._state_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        if thread is not threading.current_thread():
            thread.join()
        with self._state_lock:
            if self._thread is thread:
                self._thread = None

    def run_forever(self) -> None:
        self._emit("worker_started")
        try:
            while not self._stop.is_set():
                observed_at = self._now()
                try:
                    self.tick(observed_at)
                except Exception as error:  # noqa: BLE001 - worker must survive one tick
                    self._emit(
                        "worker_tick_failed",
                        error_type=type(error).__name__,
                        error=str(error),
                    )
                self._stop.wait(self._poll_seconds)
        finally:
            self._emit("worker_stopped")

    def tick(self, observed_at: datetime) -> WorkerTick:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("worker observation must be timezone-aware")
        target, reason = self._target(observed_at)
        phases = tuple(self._clock.phase_at(exchange, observed_at) for exchange in ("SSE", "SZSE"))
        session = (phases[0], phases[1])
        if session != self._last_session_state:
            self._last_session_state = session
            self._emit(
                "session_state",
                observed_at=format_rfc3339(observed_at),
                sse_phase=phases[0],
                szse_phase=phases[1],
            )
        if target is None:
            return WorkerTick("NO_CYCLE", reason=reason)

        market_target = target.astimezone(_MARKET_TIMEZONE)
        cycle_key = f"{market_target.date().isoformat()}:{market_target.strftime('%H:%M')}"
        with self._state_lock:
            if cycle_key == self._last_cycle_key:
                return WorkerTick("SKIPPED_DUPLICATE", cycle_key=cycle_key, target_minute=target)
        if not self._cycle_lock.acquire(blocking=False):
            self._emit(
                "cycle_skipped",
                reason="ACTIVE_CYCLE",
                observed_at=format_rfc3339(observed_at),
                target_minute=format_rfc3339(target),
            )
            return WorkerTick(
                "SKIPPED_BUSY", cycle_key=cycle_key, target_minute=target, reason="ACTIVE_CYCLE"
            )

        try:
            with self._state_lock:
                self._last_cycle_key = cycle_key
            if self._run_cycle is None:
                self._emit(
                    "cycle_skipped",
                    reason="WORKER_DISABLED",
                    observed_at=format_rfc3339(observed_at),
                    target_minute=format_rfc3339(target),
                )
                return WorkerTick(
                    "SKIPPED_DISABLED",
                    cycle_key=cycle_key,
                    target_minute=target,
                    reason="WORKER_DISABLED",
                )
            started = time.monotonic()
            self._emit(
                "cycle_started",
                observed_at=format_rfc3339(observed_at),
                target_minute=format_rfc3339(target),
                cycle_key=cycle_key,
            )
            try:
                result = self._run_cycle(cycle_key, observed_at)
            except Exception as error:  # noqa: BLE001 - isolate one market cycle
                duration_ms = int((time.monotonic() - started) * 1000)
                self._emit(
                    "cycle_failed",
                    observed_at=format_rfc3339(observed_at),
                    target_minute=format_rfc3339(target),
                    cycle_key=cycle_key,
                    cycle_duration_ms=duration_ms,
                    next_target=self._next_target(observed_at, target),
                    **_stage_timing(None),
                    error_type=type(error).__name__,
                    error=str(error),
                )
                return WorkerTick(
                    "FAILED",
                    cycle_key=cycle_key,
                    target_minute=target,
                    reason=f"{type(error).__name__}: {error}",
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            self._emit(
                "cycle_completed",
                observed_at=format_rfc3339(observed_at),
                target_minute=format_rfc3339(target),
                cycle_key=cycle_key,
                cycle_duration_ms=duration_ms,
                next_target=self._next_target(observed_at, target),
                **_stage_timing(result),
                result_status=_result_status(result),
            )
            return WorkerTick("COMPLETED", cycle_key=cycle_key, target_minute=target, result=result)
        finally:
            self._cycle_lock.release()

    def _next_target(self, observed_at: datetime, current: datetime) -> str | None:
        target, _ = self._target(observed_at + timedelta(minutes=1))
        if target is None or target <= current:
            return None
        return format_rfc3339(target)

    def _target(self, observed_at: datetime) -> tuple[datetime | None, str]:
        phases = tuple(self._clock.phase_at(exchange, observed_at) for exchange in ("SSE", "SZSE"))
        if phases[0] != phases[1]:
            return None, "EXCHANGE_PHASE_MISMATCH"
        if phases[0] in self._CONTINUOUS_PHASES:
            targets = tuple(
                self._clock.latest_completed_continuous_minute(exchange, observed_at)
                for exchange in ("SSE", "SZSE")
            )
            if targets[0] is None or targets[1] is None:
                return None, "NO_COMPLETED_LEGAL_MINUTE"
            if targets[0] != targets[1]:
                return None, "EXCHANGE_TARGET_MISMATCH"
            return targets[0], "READY"

        # A close timestamp is a legal completed right edge even though phase_at
        # transitions to BREAK/CLOSED at the exclusive session boundary.
        targets = tuple(
            self._clock.latest_completed_legal_minute(exchange, observed_at)
            for exchange in ("SSE", "SZSE")
        )
        if targets[0] is None or targets[0] != targets[1]:
            return None, "SESSION_NOT_CONTINUOUS"
        target = targets[0]
        if observed_at - target > self._close_grace:
            return None, "SESSION_NOT_CONTINUOUS"
        if observed_at.replace(second=0, microsecond=0) != target:
            return None, "SESSION_NOT_CONTINUOUS"
        return target, "READY_AT_SESSION_CLOSE"

    def _emit(self, event: str, **fields: object) -> None:
        payload = {"event": event, **fields}
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _result_status(result: object) -> str | None:
    status = getattr(result, "status", None)
    if status is None:
        return None
    value = getattr(status, "value", status)
    return str(value)


def _stage_timing(result: object) -> dict[str, object]:
    details = getattr(result, "details", None)
    if not isinstance(details, Mapping):
        details = {}
    return {
        "acquisition_duration_ms": details.get("acquisition_duration_ms"),
        "analysis_duration_ms": details.get("analysis_duration_ms"),
        "commit_duration_ms": details.get("commit_duration_ms"),
    }
