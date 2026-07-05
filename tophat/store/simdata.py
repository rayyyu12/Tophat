"""Loader for the Simulation page's historical bar cache.

The cache (research/reconstruction/sim_ticks_rth_1s.parquet, built by
research/reconstruction/export_sim_ticks.py from the NinjaTrader TICK db)
holds the rolled front-month NQ RTH bars at 1-second resolution - the original
research fidelity (data/cache/nq_rth_1s.parquet). This module turns them into
per-day entry views for an arbitrary entry time: drive direction measured
09:30 open -> last close before entry, fill at the entry bar's open, and the
high/low path from entry to the 16:00 close for first-touch resolution.

Times are "HH:MM:SS" ET wall clock ("HH:MM" in older caches is normalized on
load). Bars load once per process; day views are cached per entry time.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tophat.store.paths import SIM_BARS_FILE

RTH_OPEN = "09:30:00"
RTH_CLOSE = "16:00:00"
# Completeness gates (from the reconstruction pipeline): a usable day needs a
# formed opening range and a real post-entry session.
MIN_RANGE_BARS = 10
MIN_POST_BARS = 60


@dataclass(frozen=True)
class DayBars:
    """One trading day's RTH minute bars (parallel arrays, ET wall-clock times)."""
    date: str
    times: np.ndarray    # "HH:MM" strings
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


@dataclass(frozen=True)
class DayView:
    """A day as seen from one entry time: drive direction + post-entry path."""
    date: str
    direction: int       # sign(close just before entry - 09:30 open); 0 = flat
    entry: float         # entry bar open (the fill)
    hi: np.ndarray       # highs entry..16:00
    lo: np.ndarray       # lows  entry..16:00
    eod_close: float


_LOCK = threading.Lock()
_BARS: list[DayBars] | None = None
_BARS_PATH: Path | None = None
_VIEWS: dict[str, list[DayView]] = {}


def available(path: Path = SIM_BARS_FILE) -> bool:
    return path.exists()


def reset_cache() -> None:
    """Drop the in-process bar cache (tests; after regenerating the parquet)."""
    global _BARS, _BARS_PATH
    with _LOCK:
        _BARS = None
        _BARS_PATH = None
        _VIEWS.clear()


def load_days(path: Path = SIM_BARS_FILE) -> list[DayBars]:
    """All cached days, oldest first. Loaded once per process per path."""
    global _BARS, _BARS_PATH
    with _LOCK:
        if _BARS is not None and _BARS_PATH == path:
            return _BARS
        import pandas as pd
        df = pd.read_parquet(path)
        # normalize "HH:MM" (older minute-era caches, tests) to "HH:MM:SS"
        t = df["time"].astype(str)
        df["time"] = np.where(t.str.len() == 5, t + ":00", t)
        days: list[DayBars] = []
        for date, g in df.groupby("date", sort=True):
            days.append(DayBars(
                date=str(date),
                times=g["time"].to_numpy(),
                open=g["open"].to_numpy(dtype=float),
                high=g["high"].to_numpy(dtype=float),
                low=g["low"].to_numpy(dtype=float),
                close=g["close"].to_numpy(dtype=float),
            ))
        _BARS, _BARS_PATH = days, path
        _VIEWS.clear()
        return days


def day_views(entry_hhmm: str, path: Path = SIM_BARS_FILE) -> list[DayView]:
    """Per-day entry views for `entry_hhmm` (ET). Days without a formed opening
    range or a real post-entry session are skipped, matching the backtest."""
    days = load_days(path)
    entry = entry_hhmm + ":00" if len(entry_hhmm) == 5 else entry_hhmm
    with _LOCK:
        if entry in _VIEWS:
            return _VIEWS[entry]
    views: list[DayView] = []
    for d in days:
        rng = (d.times >= RTH_OPEN) & (d.times < entry)
        post = (d.times >= entry) & (d.times <= RTH_CLOSE)
        n_rng, n_post = int(rng.sum()), int(post.sum())
        if n_rng < MIN_RANGE_BARS or n_post < MIN_POST_BARS:
            continue
        or_open = float(d.open[rng][0])
        or_close = float(d.close[rng][-1])
        direction = 0 if or_close == or_open else (1 if or_close > or_open else -1)
        views.append(DayView(
            date=d.date,
            direction=direction,
            entry=float(d.open[post][0]),
            hi=d.high[post],
            lo=d.low[post],
            eod_close=float(d.close[post][-1]),
        ))
    with _LOCK:
        _VIEWS[entry] = views
    return views


def coverage(path: Path = SIM_BARS_FILE) -> dict:
    """Availability metadata for the UI (no exception when the cache is absent)."""
    if not available(path):
        return {"available": False, "days": 0, "date_from": "", "date_to": ""}
    days = load_days(path)
    return {"available": True, "days": len(days),
            "date_from": days[0].date if days else "",
            "date_to": days[-1].date if days else ""}
