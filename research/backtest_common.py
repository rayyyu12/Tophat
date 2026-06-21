"""Shared drive-momentum backtest utilities (1s RTH cache)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

CACHE_FILE = "data/cache/nq_rth_1s.parquet"
OR_START, OR_END = "09:30", "09:44:59"
ENTRY_START, ENTRY_END = "09:45", "10:15"


@dataclass(frozen=True)
class Bracket:
    name: str
    target_pts: float
    stop_pts: float

    @property
    def baseline(self) -> float:
        return self.stop_pts / (self.target_pts + self.stop_pts)


@dataclass
class DayData:
    date: object
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    secs: np.ndarray
    entry_idx: np.ndarray
    or_open: float
    or_close: float


def load_days(path: str = CACHE_FILE) -> list[DayData]:
    df = pd.read_parquet(path)
    days: list[DayData] = []
    for d, g in df.groupby("date", sort=True):
        g = g.sort_index()
        win = g.index.indexer_between_time(ENTRY_START, ENTRY_END)
        orng = g.index.indexer_between_time(OR_START, OR_END)
        if len(win) == 0 or len(orng) == 0:
            continue
        hi = g["high"].to_numpy(dtype="float64")
        lo = g["low"].to_numpy(dtype="float64")
        op = g["open"].to_numpy(dtype="float64")
        cl = g["close"].to_numpy(dtype="float64")
        days.append(DayData(
            date=d, open=op, high=hi, low=lo, close=cl,
            secs=(g.index.asi8 // 1_000_000_000).astype("int64"),
            entry_idx=np.asarray(win, dtype="int64"),
            or_open=float(op[orng[0]]),
            or_close=float(cl[orng[-1]]),
        ))
    return days


def drive_entry(day: DayData):
    """Enter at 09:45 ET in the direction of the 09:30-09:45 opening move."""
    if day.or_close > day.or_open:
        return int(day.entry_idx[0]), 1
    if day.or_close < day.or_open:
        return int(day.entry_idx[0]), -1
    return None


def collect_drive_entries(days: list[DayData]):
    out = []
    for day in days:
        e = drive_entry(day)
        if e is not None:
            out.append((day, e[0], e[1]))
    return out


def resolve(day: DayData, entry_i: int, direction: int, br: Bracket,
            slip: float = 0.0) -> str:
    """Returns 'win' | 'loss' | 'unresolved'. Fill = next bar open after signal."""
    fill_i = entry_i + 1
    if fill_i >= day.open.size:
        return "unresolved"
    entry = day.open[fill_i] + slip if direction == 1 else day.open[fill_i] - slip
    hi = day.high[fill_i:]
    lo = day.low[fill_i:]
    if hi.size == 0:
        return "unresolved"
    if direction == 1:
        tgt = hi >= entry + br.target_pts
        stp = lo <= entry - br.stop_pts
    else:
        tgt = lo <= entry - br.target_pts
        stp = hi >= entry + br.stop_pts
    t_any, s_any = tgt.any(), stp.any()
    if not t_any and not s_any:
        return "unresolved"
    if t_any and not s_any:
        return "win"
    if s_any and not t_any:
        return "loss"
    return "win" if int(tgt.argmax()) < int(stp.argmax()) else "loss"


def tally(results: list[str]) -> dict:
    c = {"win": 0, "loss": 0, "unresolved": 0}
    for r in results:
        c[r] += 1
    return c


def run_coinflip(days: list[DayData], br: Bracket, samples_per_day: int,
                 rng: np.random.Generator, slip: float = 0.0) -> dict:
    res = []
    for day in days:
        for _ in range(samples_per_day):
            i = int(rng.choice(day.entry_idx))
            res.append(resolve(day, i, 1 if rng.random() < 0.5 else -1, br, slip))
    return tally(res)


def run_drive(entries, br: Bracket, slip: float = 0.0) -> dict:
    return tally([resolve(day, i, d, br, slip) for (day, i, d) in entries])


def monthly_breakdown(entries, br: Bracket, slip: float = 0.0) -> dict:
    months: dict[str, list[int]] = {}
    for day, i, d in entries:
        r = resolve(day, i, d, br, slip)
        if r == "unresolved":
            continue
        m = str(day.date)[:7]
        wl = months.setdefault(m, [0, 0])
        wl[1] += 1
        if r == "win":
            wl[0] += 1
    return {m: (w, n) for m, (w, n) in months.items()}


def wilson_ci(wins: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def fmt(c: dict) -> str:
    resolved = c["win"] + c["loss"]
    total = resolved + c["unresolved"]
    wr = c["win"] / resolved if resolved else 0.0
    lo, hi = wilson_ci(c["win"], resolved)
    unres = c["unresolved"] / total if total else 0.0
    return (f"{wr:>6.1%}  [{lo:>5.1%},{hi:>5.1%}]  "
            f"n={resolved:>5,}  unres={unres:>4.1%}")


def edge_str(c: dict, br: Bracket) -> str:
    n = c["win"] + c["loss"]
    return f"{(c['win']/n - br.baseline):+.1%}" if n else "  n/a"
