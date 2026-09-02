from __future__ import annotations

from threading import Event, Lock, Thread, current_thread
from typing import Protocol


class DeliveryRunner(Protocol):
    def run_once(self) -> bool: ...


class DeliveryLoop:
    """Own one bounded delivery thread and stop it before its WriterQueue is closed."""

    def __init__(self, worker: DeliveryRunner, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds < 0.05 or poll_seconds > 60:
            raise ValueError("delivery poll interval is outside the accepted bounds")
        self._worker = worker
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> bool:
        return self._worker.run_once()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = Thread(
                target=self._run,
                name="market-monitor-delivery",
                daemon=False,
            )
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        if thread is not current_thread():
            thread.join()
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self._poll_seconds)
