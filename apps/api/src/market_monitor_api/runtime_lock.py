"""Cross-process ownership lock for the local one-writer runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast


class RuntimeLockError(RuntimeError):
    """Raised when another local runtime already owns a data directory."""


class RuntimeLock:
    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    @classmethod
    def acquire(cls, data_directory: Path) -> RuntimeLock:
        directory = data_directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(directory / ".market-monitor-runtime.lock", os.O_RDWR | os.O_CREAT)
        try:
            os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            _lock(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise RuntimeLockError("another local runtime is already active") from error
        return cls(descriptor)

    def close(self) -> None:
        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)


def _lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    fcntl = cast(Any, __import__("fcntl"))

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl = cast(Any, __import__("fcntl"))

    fcntl.flock(descriptor, fcntl.LOCK_UN)
