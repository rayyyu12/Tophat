"""Background auto-fire scheduler.

A single asyncio loop that, on trading days during the morning window, calls the
session runner every ~30s. The runner is idempotent — it reconciles closed trades
and fires each account exactly once when its stagger slot arrives (guarded by
last_fire_date), so repeated calls just pick up whatever is now due.

Timing: the drive direction is fully determined at 09:44:59 ET (the opening range
closes), so entries must go out AT the entry time, not up to an interval later.
The loop therefore aligns its sleep to land a tick within ~50ms after each
configured entry time, and starts ticking a couple of minutes early (PREP_MIN) so
the first real tick runs on a warm HTTP connection with reconciliation done.

Gated by settings.auto_execute: when disarmed, the loop runs but places nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger("tophat.automation")

WINDOW_END = "11:30"   # ET; after this, the morning's entries are done
PREP_MIN = 2           # start ticking this many minutes before the first entry


def _parse_hhmm(t: str) -> tuple[int, int] | None:
    """'HH:MM' (or 'H:MM') -> (hour, minute), else None."""
    try:
        hh_s, mm_s = str(t).strip().split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        return None
    if 0 <= hh < 24 and 0 <= mm < 60:
        return hh, mm
    return None


def _entry_times(settings) -> list[tuple[int, int]]:
    raw = list(settings.flip_stagger_times) + [settings.nuke_entry_time]
    parsed = sorted({p for t in raw if (p := _parse_hhmm(t)) is not None})
    return parsed or [(9, 45)]


class Automation:
    def __init__(self, pool_provider, interval: float = 30.0) -> None:
        # `pool_provider(uid)` returns that user's current broker pool, so adding/
        # removing an API key in Settings is picked up on the next tick without a
        # restart. Every login user with auto_execute armed gets their own tick —
        # users run concurrently (independent files/brokers; see service._run_lock).
        self.pool_provider = pool_provider
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_tick: str | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None
        # uid -> YYYY-MM-DD the nightly OCO probe last ran. In-memory on purpose:
        # a restart re-probing the same night is a harmless duplicate check.
        self._probe_done: dict[int, str] = {}
        self.last_probe: dict | None = None

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
        first_h, first_m = _entry_times(settings)[0]
        start = (now.replace(hour=first_h, minute=first_m, second=0, microsecond=0)
                 - timedelta(minutes=PREP_MIN))
        end_h, end_m = _parse_hhmm(WINDOW_END) or (11, 30)
        end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return start <= now <= end

    def _next_entry_delay(self, now: datetime, settings) -> float | None:
        """Seconds until the next entry time today, or None if none remain."""
        best: float | None = None
        for hh, mm in _entry_times(settings):
            entry = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delta = (entry - now).total_seconds()
            if delta > 0 and (best is None or delta < best):
                best = delta
        return best

    async def _tick_user(self, uid: int, now: datetime) -> dict:
        """One user's automation tick, in that user's tenant context.

        Runs as its own asyncio task, so the ContextVar set here is task-local
        and asyncio.to_thread carries it into the session runner's thread."""
        from tophat.server import service
        from tophat.store import tenant
        from tophat.store.config import load_settings
        tenant.set_user(uid)
        out: dict = {"uid": uid, "fired": False, "result": None, "next_delay": None}
        settings = load_settings()
        if settings.auto_execute and self._in_window(now, settings):
            log.info("automation tick — firing uid=%s sessions at %s ET",  # [debuglog]
                     uid, now.strftime("%H:%M:%S"))
            res = await asyncio.to_thread(
                service.run_all_sessions, self.pool_provider(uid),
                execute=True, respect_times=True, now_et=now)
            out["fired"], out["result"] = True, res
            log.info("automation tick done — uid=%s placed=%s reconciled=%s",  # [debuglog]
                     uid, res.get("orders_placed"), len(res.get("reconciled", [])))
        # Nightly Auto-OCO probe: one shot per user per night at oco_probe_time
        # (Sun-Thu, evening session). Marked done even on failure — a probe is
        # advisory; the morning fire path still alerts on a live rejection. The
        # stamp is persisted so a restart after the probe won't re-run it.
        from tophat.services import oco_probe
        if uid not in self._probe_done:      # cold start: seed from disk
            self._probe_done[uid] = oco_probe.load_stamp()
        if settings.auto_execute and oco_probe.probe_due(
                now, getattr(settings, "oco_probe_time", ""),
                self._probe_done[uid]):
            self._probe_done[uid] = now.strftime("%Y-%m-%d")
            oco_probe.save_stamp(self._probe_done[uid])
            log.info("nightly OCO probe — uid=%s at %s ET", uid, now.strftime("%H:%M:%S"))
            try:
                self.last_probe = await asyncio.to_thread(
                    oco_probe.run_probe, self.pool_provider(uid), now_et=now)
            except Exception:
                log.exception("nightly OCO probe failed uid=%s", uid)
        out["next_delay"] = self._next_entry_delay(datetime.now(ET), settings)
        return out

    async def _loop(self) -> None:
        from tophat.server import auth
        while not self._stop.is_set():
            timeout = self.interval
            try:
                now = datetime.now(ET)
                users = auth.list_users()
                ticks = await asyncio.gather(
                    *(self._tick_user(int(u["id"]), now) for u in users),
                    return_exceptions=True)
                fired = [t for t in ticks if isinstance(t, dict) and t["fired"]]
                errors = [t for t in ticks if isinstance(t, Exception)]
                if fired:
                    self.last_tick = now.strftime("%Y-%m-%d %H:%M:%S ET")
                    self.last_result = {
                        "orders_placed": sum(t["result"].get("orders_placed", 0)
                                             for t in fired),
                        "reconciled": [r for t in fired
                                       for r in t["result"].get("reconciled", [])],
                        "users": {t["uid"]: t["result"] for t in fired},
                    }
                if errors:
                    self.last_error = str(errors[0])
                    for e in errors:
                        log.error("automation user tick failed: %s", e)
                elif fired:
                    self.last_error = None
                # Align the next wake-up to the soonest entry time across all users
                # when it lands inside this sleep, so ticks fire AT 09:45:00 (+~50ms),
                # not up to an interval later. (+0.05s lands just past the minute
                # boundary so the HH:MM due-time comparison passes.)
                delays = [t["next_delay"] for t in ticks
                          if isinstance(t, dict) and t["next_delay"] is not None]
                delay = min(delays) if delays else None
                if delay is not None and 0 < delay < timeout:
                    timeout = delay + 0.05
            except Exception as exc:           # never let the loop die
                self.last_error = str(exc)
                log.exception("automation tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
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
