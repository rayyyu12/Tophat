"""TEMPORARY verbose debug logging (remove later).

Writes a detailed, timestamped run log to a project-local `logs/` folder so we can
see exactly what happened after the fact — every ProjectX request/response and every
trading decision (orders placed, bracket prices, fills, reconciliations, blows).

Design goals:
  * NON-BLOCKING — log records go on an in-memory queue; one background thread does
    the file writes (QueueListener). The trading/hot paths never wait on disk I/O.
  * KILL-SAFE — each record is flushed as it's written, and `stop()` (registered via
    atexit and called from the server lifespan) drains the queue on shutdown. A hard
    SIGKILL loses at most the handful of records still in the queue.
  * ZERO-COST WHEN OFF — if `start()` isn't called (or TOPHAT_DEBUG_LOG=0), the
    `tophat.*` loggers sit at the default WARNING level, so the info/debug calls
    sprinkled through the code are level-checked no-ops (no formatting, no I/O).

TO REMOVE THIS FEATURE LATER:
  1. delete this module,
  2. delete the `debuglog.start()/stop()` calls in tophat/server/app.py,
  3. delete the `logging.getLogger("tophat.trade"|"tophat.projectx")` lines and the
     log.* calls in service.py / client.py (all greppable: `# [debuglog]`).

Disable at runtime without removing code: set env TOPHAT_DEBUG_LOG=0.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
ROOT_LOGGER = "tophat"

_listener: logging.handlers.QueueListener | None = None
_qhandler: logging.Handler | None = None
_started = False
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("TOPHAT_DEBUG_LOG", "1").strip().lower() not in ("0", "false", "no", "off")


def log(name: str = "tophat.debug") -> logging.Logger:
    return logging.getLogger(name)


def start() -> Path | None:
    """Begin writing the debug log. Idempotent; returns the log file path (or None)."""
    global _listener, _qhandler, _started
    with _lock:
        if _started or not enabled():
            return None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"tophat-{datetime.now():%Y%m%d-%H%M%S}-pid{os.getpid()}.log"

        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-16s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))

        log_queue: queue.SimpleQueue = queue.SimpleQueue()
        _qhandler = logging.handlers.QueueHandler(log_queue)
        root = logging.getLogger(ROOT_LOGGER)
        root.setLevel(logging.DEBUG)
        root.addHandler(_qhandler)

        _listener = logging.handlers.QueueListener(
            log_queue, file_handler, respect_handler_level=True)
        _listener.start()
        _started = True
        _install_excepthooks()
        atexit.register(stop)
        log().info("===== debug log started — pid=%s python=%s file=%s =====",
                   os.getpid(), sys.version.split()[0], path.name)
        return path


def stop() -> None:
    """Flush and close the debug log. Safe to call repeatedly / from atexit."""
    global _listener, _qhandler, _started
    with _lock:
        if not _started:
            return
        log().info("===== debug log stopping — pid=%s =====", os.getpid())
        root = logging.getLogger(ROOT_LOGGER)
        if _listener is not None:
            _listener.stop()          # drains the queue into the file, then joins the thread
            for h in _listener.handlers:
                try:
                    h.close()
                except Exception:
                    pass
            _listener = None
        if _qhandler is not None:
            root.removeHandler(_qhandler)
            _qhandler = None
        _started = False


def _install_excepthooks() -> None:
    """Funnel uncaught exceptions (main + threads) into the log so a crash is visible."""
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        log("tophat.crash").critical("UNCAUGHT exception", exc_info=(exc_type, exc, tb))
        prev(exc_type, exc, tb)

    sys.excepthook = hook

    prev_thread = threading.excepthook

    def thread_hook(args):
        name = args.thread.name if args.thread else "?"
        log("tophat.crash").critical("UNCAUGHT thread exception in %s", name,
                                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        prev_thread(args)

    threading.excepthook = thread_hook


def redact(body: object) -> object:
    """Strip secrets before logging a request body."""
    if not isinstance(body, dict):
        return body
    secret = {"apikey", "api_key", "password", "token", "newtoken"}
    return {k: ("***" if k.lower() in secret else v) for k, v in body.items()}
