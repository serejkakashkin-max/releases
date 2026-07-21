from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO, Optional


class FileLockTimeoutError(TimeoutError):
    pass


class CrossProcessFileLock:
    """Small OS-backed exclusive lock that works on Windows and Linux."""

    def __init__(self, path: Path, *, timeout: float = 10.0, poll_interval: float = 0.05):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self._handle: Optional[BinaryIO] = None

    def __enter__(self) -> "CrossProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._acquire_os_lock(handle)
                    self._handle = handle
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise FileLockTimeoutError(
                            "Timed out while waiting for the employee directory lock."
                        )
                    time.sleep(self.poll_interval)
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._release_os_lock(handle)
        finally:
            self._handle = None
            handle.close()

    @staticmethod
    def _acquire_os_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release_os_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
