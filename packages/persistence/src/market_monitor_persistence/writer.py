import sqlite3
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Full, Queue
from threading import Lock, Thread, get_ident
from typing import Any, TypeVar, cast

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from market_monitor_persistence.database import DatabaseRuntime

T = TypeVar("T")


class WriterQueueClosedError(RuntimeError):
    """Raised when work is submitted after shutdown."""


class WriterQueueReentrancyError(RuntimeError):
    """Raised when a writer command tries to submit nested work."""


class DatabaseBusyError(RuntimeError):
    """Raised when SQLite cannot acquire its write lock."""


class WriterQueueFullError(RuntimeError):
    """Raised when bounded writer backpressure times out."""


@dataclass(frozen=True)
class TransactionContext:
    connection: Connection


class WriterQueue:
    def __init__(self, runtime: DatabaseRuntime, maxsize: int = 1024) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._runtime = runtime
        self._queue: Queue[_WorkItem[Any] | object] = Queue(maxsize=maxsize)
        self._stop = object()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._worker_ident: int | None = None
        self._accepting = False

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                return
            self._accepting = True
            self._thread = Thread(target=self._run, name="market-monitor-writer", daemon=False)
            self._thread.start()

    def submit(
        self, command: Callable[[TransactionContext], T], timeout: float | None = None
    ) -> Future[T]:
        if get_ident() == self._worker_ident:
            raise WriterQueueReentrancyError("writer commands cannot submit nested writes")
        with self._state_lock:
            if not self._accepting:
                raise WriterQueueClosedError("writer queue is not accepting work")
            future: Future[T] = Future()
            try:
                if timeout is None:
                    self._queue.put(_WorkItem(command=command, future=future))
                else:
                    self._queue.put(_WorkItem(command=command, future=future), timeout=timeout)
            except Full as error:
                raise WriterQueueFullError("writer queue is full") from error
        return future

    def barrier(self) -> None:
        self.submit(lambda _: None).result()

    def close(self) -> None:
        with self._state_lock:
            thread = self._thread
            if thread is None:
                self._accepting = False
                return
            if self._accepting:
                self._accepting = False
                self._queue.put(self._stop)
        thread.join()
        with self._state_lock:
            self._thread = None
            self._worker_ident = None

    def _run(self) -> None:
        self._worker_ident = get_ident()
        while True:
            item = self._queue.get()
            try:
                if item is self._stop:
                    return
                work = cast(_WorkItem[Any], item)
                if not work.future.set_running_or_notify_cancel():
                    continue
                try:
                    with self._runtime._writer_engine.begin() as connection:
                        result = work.command(TransactionContext(connection))
                except OperationalError as error:
                    if _is_busy(error):
                        busy = DatabaseBusyError("SQLite writer lock is busy")
                        busy.__cause__ = error
                        work.future.set_exception(busy)
                    else:
                        work.future.set_exception(error)
                except BaseException as error:
                    work.future.set_exception(error)
                else:
                    work.future.set_result(result)
            finally:
                self._queue.task_done()


@dataclass(frozen=True)
class _WorkItem[T]:
    command: Callable[[TransactionContext], T]
    future: Future[T]


def _is_busy(error: OperationalError) -> bool:
    original = error.orig
    return isinstance(original, sqlite3.OperationalError) and original.sqlite_errorcode in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }
