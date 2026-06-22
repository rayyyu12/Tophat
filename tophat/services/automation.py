"""Background auto-fire scheduler.

A single asyncio loop that, on trading days during the morning window, calls the
session runner every ~30s. The runner is idempotent — it reconciles closed trades
and fires each account exactly once when its stagger slot arrives (guarded by
last_fire_date), so repeated calls just pick up whatever is now due.

Gated by settings.auto_execute: when disarmed, the loop runs but places nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger("tophat.automation")

WINDOW_END = "11:30"   # ET; after this, the morning's entries are done


class Automation:
    def __init__(self, pool_provider, interval: float = 30.0) -> None:
        # `pool_provider` returns the current broker pool, so adding/removing an
        # API key in Settings is picked up on the next tick without a restart.
        self.pool_provider = pool_provider
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_tick: str | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    def _in_window(self, now: datetime, settings) -> bool:
        if now.weekday() >= 5:          # Sat/Sun (US market holidays self-skip: no drive)
            return False
        times = list(settings.flip_stagger_times) + [settings.nuke_entry_time]
        first = min(times) if times else "09:45"
        return first <= now.strftime("%H:%M") <= WINDOW_END

    async def _loop(self) -> None:
        from tophat.server import service
        from tophat.store.config import load_settings
        while not self._stop.is_set():
            try:
                settings = load_settings()
                now = datetime.now(ET)
                if settings.auto_execute and self._in_window(now, settings):
                    res = await asyncio.to_thread(
                        service.run_all_sessions, self.pool_provider(),
                        execute=True, respect_times=True, now_et=now)
                    self.last_tick = now.strftime("%Y-%m-%d %H:%M:%S ET")
                    self.last_result = res
                    self.last_error = None
            except Exception as exc:           # never let the loop die
                self.last_error = str(exc)
                log.exception("automation tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_tick": self.last_tick,
            "orders_last_tick": (self.last_result or {}).get("orders_placed", 0),
            "reconciled_last_tick": len((self.last_result or {}).get("reconciled", [])),
            "last_error": self.last_error,
        }
