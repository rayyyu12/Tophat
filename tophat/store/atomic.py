"""Atomic file writes.

Write to a temp file in the same directory, fsync, then `os.replace()` into
place. `os.replace` is atomic on both POSIX and Windows for same-filesystem
paths, so a concurrent reader sees either the old complete file or the new
complete file — never a torn, half-written one. Combined with the single-instance
lock (`store.single_instance`, one writer at a time), this removes the
shared-state corruption that let concurrent TopHat instances clobber each other's
account / schedule / registry state.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Durably replace `path`'s contents with `text` in one atomic step."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # PID in the temp name so two writers (belt-and-suspenders; the instance lock
    # already serializes them) can never collide on the same temp file.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # On Windows os.replace can transiently fail if a reader holds the
        # destination open; readers open/close quickly, so a short retry wins.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
