"""Cross-process single-instance guard.

Only one TopHat process may run at a time, because every instance shares one
ProjectX API key and one `data/` directory. Two automation loops firing the same
account is exactly what caused the 2026-06-22 incident (concurrent
close_contract / place_bracket churning a position for a few dollars of fees,
while racing JSON writes clobbered the recorded state).

The guard is an advisory exclusive lock on a file in the data dir. The OS holds
the lock for the life of the process and releases it automatically when the
process exits — including a hard crash — so there is no stale-PID file to detect
or clean up. Acquire once at startup and keep the handle alive for the whole
process lifetime.

Disable with `TOPHAT_SINGLE_INSTANCE=0` (used by the test suite, which runs many
short-lived app instances serially in one process).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tophat.store.paths import DATA_DIR

LOCK_FILE = DATA_DIR / "tophat.lock"
_ENV_FLAG = "TOPHAT_SINGLE_INSTANCE"


class AlreadyRunning(RuntimeError):
    def __init__(self, info: str = "") -> None:
        detail = f" ({info})" if info else ""
        super().__init__(
            "Another TopHat instance is already running" + detail + ". "
            "Refusing to start a second instance — multiple instances share one "
            "API key and one data directory and will race each other's trades. "
            "Stop the other instance first (or set TOPHAT_SINGLE_INSTANCE=0 to "
            "override, not recommended)."
        )
        self.info = info


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # non-blocking exclusive, 1 byte

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    """An OS file lock held for the process lifetime (keeps the fd open)."""

    def __init__(self, path: Path = LOCK_FILE) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT without truncation: a previous holder's pid stays readable for
        # diagnostics until we win and overwrite it.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            info = _read_holder(fd)
            os.close(fd)
            raise AlreadyRunning(info)
        # We own the lock — stamp our pid for humans inspecting the file.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                _unlock(self._fd)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def _read_holder(fd: int) -> str:
    """Best-effort read of the holder's pid stamp (may be empty if locked)."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 256).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def enabled() -> bool:
    return os.getenv(_ENV_FLAG, "1").lower() not in ("0", "false", "no")


def acquire_or_none(path: Path = LOCK_FILE) -> InstanceLock | None:
    """Acquire the singleton lock unless disabled by env. Raises AlreadyRunning
    if another instance holds it; returns None only when the guard is disabled."""
    if not enabled():
        return None
    return InstanceLock(path).acquire()
