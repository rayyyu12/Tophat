"""
Backtest execution/bracket engine for the TopHat pipeline.

Loads the 1-second RTH cache and, for each trading day, enters a bracketed trade
during 09:45-10:15 ET, then resolves it against subsequent 1s bars until target
or stop is touched. Two entry modes share the SAME resolution logic:

  - coinflip : random entry time + random direction. NO-EDGE control; reproduces
               the geometric baseline b/(a+b). The bar any signal must beat.
  - signal   : pre-registered liquidity-sweep reversal (see signal_entry).

Pre-registered signal (committed BEFORE measuring; no tuning to the data):
  - Reference: high/low of the 09:30-09:45 ET opening range (OR).
  - Sweep: during 09:45-10:15, price trades >= 1 tick beyond the OR.
  - Reclaim: within 5 minutes of the sweep, a 1s bar CLOSES back inside the OR.
  - Entry: fade the sweep (short after a high sweep, long after a low sweep),
           on the reclaim bar. First valid setup per day only.
  - Exit: phase bracket, identical resolution to the control.

Resolution choices: unresolved-by-16:00 excluded from win rate; intrabar
target+stop tie resolves to the STOP (pessimistic).

Usage:
    python backtest.py
    python backtest.py --samples-per-day 40 --seed 1
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

CACHE_FILE = "data/cache/nq_rth_1s.parquet"
OR_START, OR_END = "09:30", "09:44:59"
ENTRY_START, ENTRY_END = "09:45", "10:15"
TICK = 0.25
RECLAIM_SECS = 300          # 5-minute reclaim window after a sweep


@dataclass(frozen=True)
class Bracket:
    name: str
    target_pts: float
    stop_pts: float

    @property
    def baseline(self) -> float:
        return self.stop_pts / (self.target_pts + self.stop_pts)


PHASES = [
    Bracket("eval", 15.5, 9.5),       # baseline ~38%
    Bracket("flip", 7.5, 50.0),       # baseline ~87%
    Bracket("nukeNoDLL", 100.0, 50.0),  # 2:1, no-DLL nuke ($2k risk for $4k) ~33%
    Bracket("nukeDLL", 100.0, 25.0),    # 4:1, DLL nuke per-try ($1k risk for $4k) ~20%
]


@dataclass
class DayData:
    date: object
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    secs: np.ndarray          # epoch seconds per bar (for the reclaim window)
    entry_idx: np.ndarray     # indices in 09:45-10:15
    or_hi: float
    or_lo: float
    or_open: float
    or_close: float


def load_days(path: str) -> list[DayData]:
    df = pd.read_parquet(path)
    days = []
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
            or_hi=float(hi[orng].max()),
            or_lo=float(lo[orng].min()),
            or_open=float(op[orng[0]]),
            or_close=float(cl[orng[-1]]),
        ))
    return days


def resolve(day: DayData, entry_i: int, direction: int, br: Bracket,
            slip: float = 0.0) -> str:
    """direction: +1 long, -1 short. Returns 'win' | 'loss' | 'unresolved'.

    The signal is detected on bar `entry_i`; the fill happens on the NEXT bar's
    open (no same-bar look-ahead). `slip` (points) worsens the entry fill: a long
    pays slip higher, a short sells slip lower. Resolution scans from the fill bar
    onward (inclusive), so the fill bar itself can hit the target or stop.
    """
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


def sweep_entry(day: DayData):
    """Liquidity-sweep reversal (mean-reversion). Returns (entry_i, dir) or None."""
    swept_kind = None
    swept_t = 0
    for i in day.entry_idx:
        t = day.secs[i]
        if swept_kind is not None and t - swept_t > RECLAIM_SECS:
            swept_kind = None
        if swept_kind is None:
            if day.high[i] >= day.or_hi + TICK:
                swept_kind, swept_t = "high", t
            elif day.low[i] <= day.or_lo - TICK:
                swept_kind, swept_t = "low", t
        if swept_kind == "high" and day.close[i] <= day.or_hi:
            return int(i), -1          # reclaimed from above -> short
        if swept_kind == "low" and day.close[i] >= day.or_lo:
            return int(i), 1           # reclaimed from below -> long
    return None


def breakout_entry(day: DayData):
    """Opening-range breakout (momentum): trade IN the direction of the break."""
    for i in day.entry_idx:
        if day.high[i] >= day.or_hi + TICK:
            return int(i), 1           # broke above -> long
        if day.low[i] <= day.or_lo - TICK:
            return int(i), -1          # broke below -> short
    return None


def drive_entry(day: DayData):
    """Opening-drive momentum: enter at 09:45 in the direction of the OR move."""
    if day.or_close > day.or_open:
        return int(day.entry_idx[0]), 1
    if day.or_close < day.or_open:
        return int(day.entry_idx[0]), -1
    return None


SIGNALS = {
    "sweep (fade)": sweep_entry,
    "breakout (mom)": breakout_entry,
    "drive (mom)": drive_entry,
}


def collect_entries(days, fn):
    out = []
    for day in days:
        e = fn(day)
        if e is not None:
            out.append((day, e[0], e[1]))
    return out


def tally(results) -> dict:
    c = {"win": 0, "loss": 0, "unresolved": 0}
    for r in results:
        c[r] += 1
    return c


def run_coinflip(days, br, samples_per_day, rng, slip=0.0) -> dict:
    res = []
    for day in days:
        for _ in range(samples_per_day):
            i = int(rng.choice(day.entry_idx))
            res.append(resolve(day, i, 1 if rng.random() < 0.5 else -1, br, slip))
    return tally(res)


def run_signal(entries, br, slip=0.0) -> dict:
    return tally([resolve(day, i, d, br, slip) for (day, i, d) in entries])


def monthly_breakdown(entries, br, slip=0.0) -> dict:
    """Returns {YYYY-MM: (wins, resolved)} for regime inspection."""
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


def main() -> None:
    ap = argparse.ArgumentParser(description="TopHat backtest engine")
    ap.add_argument("--samples-per-day", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--slip", type=float, default=0.0,
                    help="adverse entry slippage in points (e.g. 0.25 = 1 tick)")
    ap.add_argument("--monthly", action="store_true",
                    help="print drive EVAL win rate by month (regime check)")
    args = ap.parse_args()

    slip = args.slip
    rng = np.random.default_rng(args.seed)
    days = load_days(CACHE_FILE)
    mid = len(days) // 2
    is_days, oos_days = days[:mid], days[mid:]

    # pre-collect entries per signal (entry detection is bracket-independent)
    coll = {name: collect_entries(days, fn) for name, fn in SIGNALS.items()}
    coll_is = {name: collect_entries(is_days, fn) for name, fn in SIGNALS.items()}
    coll_oos = {name: collect_entries(oos_days, fn) for name, fn in SIGNALS.items()}

    print(f"Loaded {len(days)} trading days ({days[0].date} .. {days[-1].date})")
    print(f"Fills: NEXT-bar open | entry slippage: {slip} pts")
    for name in SIGNALS:
        print(f"  {name:<15} fired on {len(coll[name])}/{len(days)} days "
              f"({len(coll[name])/len(days):.0%})")
    print(f"Entry window {ENTRY_START}-{ENTRY_END} ET\n")

    def edge_str(c, br):
        n = c["win"] + c["loss"]
        return f"{(c['win']/n - br.baseline):+.1%}" if n else "  n/a"

    for br in PHASES:
        cf = run_coinflip(days, br, args.samples_per_day, rng, slip)
        print(f"=== {br.name.upper()}  (target {br.target_pts}/stop {br.stop_pts}"
              f", baseline {br.baseline:.1%}) ===")
        print(f"  {'coinflip control':<16} : {fmt(cf)}")
        for name in SIGNALS:
            sg = run_signal(coll[name], br, slip)
            sg_is = run_signal(coll_is[name], br, slip)
            sg_oos = run_signal(coll_oos[name], br, slip)
            print(f"  {name:<16} : {fmt(sg)}   edge {edge_str(sg, br)}")
            print(f"  {'  IS / OOS':<16} : "
                  f"IS {fmt(sg_is)} | OOS {fmt(sg_oos)}")
        print()

    if args.monthly:
        print("=== drive (mom) EVAL win rate by month ===")
        mb = monthly_breakdown(coll["drive (mom)"], PHASES[0], slip)
        for m in sorted(mb):
            w, n = mb[m]
            print(f"  {m} : {w/n:>6.1%}  ({w}/{n})")


if __name__ == "__main__":
    main()
