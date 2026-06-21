"""
Compare FLIP entry strategies across pre-registered time windows.

Anti-overfitting protocol (same as backtest.py):
  - All windows declared below BEFORE measuring.
  - Each strategy compared to a coin-flip control IN THE SAME WINDOW.
  - IS/OOS = first / second half of calendar days.
  - No parameter tuning on the sample.

Pre-registered windows (ET):
  RTH (09:30-16:00 cache):
    drive_open     09:45-09:45  drive-momentum (current production signal)
    coin_open      09:45-09:45  coin-flip at open (direction control for drive)
    coin_lunch     12:00-12:30  coin-flip during lunch lull
    coin_midday    13:30-14:00  coin-flip mid-afternoon
    coin_late      15:00-15:30  coin-flip into close
    coin_wide      10:00-15:00  coin-flip random time (RTH control)

  Globex (raw ticks, 18:00-04:00 cache):
    coin_asia      20:00-02:30  coin-flip overnight Asia liquidity
    coin_evening   18:15-18:30  coin-flip at 5:15-5:30 PM Central (= 6:15-6:30 PM ET)

Usage:
    python research/backtest_flip_sessions.py
    python research/backtest_flip_sessions.py --slip 0.25
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # archived: research/archive/ -> project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from research.backtest_common import Bracket, fmt, wilson_ci
from research.backtest_flip import FLIP_LIVE
from research.archive.session_loader import (
    DayData, load_evening_reopen_days, load_globex_days, load_rth_days)

# --- pre-registered windows (do not tune on data) ---
WINDOWS = [
    ("drive_open", "rth", "09:45", "09:45", "drive"),
    ("coin_open", "rth", "09:45", "09:45", "coin"),
    ("coin_lunch", "rth", "12:00", "12:30", "coin"),
    ("coin_midday", "rth", "13:30", "14:00", "coin"),
    ("coin_late", "rth", "15:00", "15:30", "coin"),
    ("coin_wide", "rth", "10:00", "15:00", "coin"),
    ("coin_asia", "globex", "20:00", "02:30", "coin"),
    ("coin_evening", "evening", "18:15", "18:30", "coin"),
]


@dataclass(frozen=True)
class SessionSpec:
    name: str
    kind: str
    entry_start: str
    entry_end: str
    mode: str  # drive | coin


def resolve(day: DayData, entry_i: int, direction: int, br: Bracket, slip: float = 0.0) -> str:
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


def drive_entry(day: DayData, or_open: float, or_close: float):
    if or_close > or_open:
        return int(day.entry_idx[0]), 1
    if or_close < or_open:
        return int(day.entry_idx[0]), -1
    return None


def load_spec(spec: SessionSpec) -> list[DayData]:
    if spec.kind == "rth":
        return load_rth_days(spec.entry_start, spec.entry_end)
    if spec.kind == "globex":
        return load_globex_days(spec.entry_start, spec.entry_end, resolve_end="03:00")
    if spec.kind == "evening":
        return load_evening_reopen_days(spec.entry_start, spec.entry_end)
    raise ValueError(spec.kind)


def load_or_map() -> dict:
    """OR open/close for drive_open only (09:30-09:44 RTH)."""
    from research.backtest_common import load_days

    return {d.date: (d.or_open, d.or_close) for d in load_days()}


def run_coin(days: list[DayData], br: Bracket, rng: np.random.Generator, samples_per_day: int, slip: float):
    res = []
    for day in days:
        for _ in range(samples_per_day):
            i = int(rng.choice(day.entry_idx))
            d = 1 if rng.random() < 0.5 else -1
            res.append(resolve(day, i, d, br, slip))
    wins = sum(1 for r in res if r == "win")
    losses = sum(1 for r in res if r == "loss")
    unres = sum(1 for r in res if r == "unresolved")
    return {"win": wins, "loss": losses, "unresolved": unres}


def run_coin_once(days: list[DayData], br: Bracket, rng: np.random.Generator, slip: float):
    """One random entry per day (realistic daily flip)."""
    res = []
    for day in days:
        i = int(rng.choice(day.entry_idx))
        d = 1 if rng.random() < 0.5 else -1
        res.append(resolve(day, i, d, br, slip))
    wins = sum(1 for r in res if r == "win")
    losses = sum(1 for r in res if r == "loss")
    unres = sum(1 for r in res if r == "unresolved")
    return {"win": wins, "loss": losses, "unresolved": unres}


def run_drive(days: list[DayData], or_map: dict, br: Bracket, slip: float):
    res = []
    for day in days:
        oc = or_map.get(day.date)
        if oc is None:
            continue
        e = drive_entry(day, oc[0], oc[1])
        if e is None:
            continue
        res.append(resolve(day, e[0], e[1], br, slip))
    wins = sum(1 for r in res if r == "win")
    losses = sum(1 for r in res if r == "loss")
    unres = sum(1 for r in res if r == "unresolved")
    return {"win": wins, "loss": losses, "unresolved": unres}


def max_drawdown_from_results(days: list[DayData], outcomes: list[str], br: Bracket, mult: float = 20.0) -> tuple[float, int]:
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    max_consec_loss = 0
    cur_loss = 0
    for o in outcomes:
        if o == "win":
            eq += br.target_pts * mult
            cur_loss = 0
        elif o == "loss":
            eq -= br.stop_pts * mult
            cur_loss += 1
            max_consec_loss = max(max_consec_loss, cur_loss)
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return max_dd, max_consec_loss


def wr(c: dict) -> float:
    n = c["win"] + c["loss"]
    return c["win"] / n if n else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="Flip strategy comparison across sessions")
    ap.add_argument("--samples-per-day", type=int, default=40,
                    help="coin control samples per day (multi-sample mode)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--slip", type=float, default=0.0)
    ap.add_argument("--daily-coin", action="store_true",
                    help="one coin flip per day instead of multi-sample control")
    args = ap.parse_args()

    br = FLIP_LIVE
    rng = np.random.default_rng(args.seed)
    or_map = load_or_map()
    specs = [SessionSpec(*w) for w in WINDOWS]

    print("=== FLIP SESSION COMPARISON (pre-registered windows) ===")
    print(f"Bracket: {br.target_pts} pt target / {br.stop_pts} pt stop  (baseline {br.baseline:.1%})")
    print(f"Slippage: {args.slip} pt | IS/OOS = first/second half of days\n")
    hdr = (f"{'Strategy':<14} {'window':<13} {'days':>5} {'win%':>6} {'95% CI':>15} "
           f"{'edge':>6} {'IS':>6} {'OOS':>6} {'unres':>5} {'maxDD$':>8} {'maxCL':>5}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for spec in specs:
        days = load_spec(spec)
        if not days:
            print(f"{spec.name:<14} {'n/a':<13} {'0':>5}  (no data)")
            continue

        mid = len(days) // 2
        is_days, oos_days = days[:mid], days[mid:]

        if spec.mode == "drive":
            sg = run_drive(days, or_map, br, args.slip)
            sg_is = run_drive(is_days, or_map, br, args.slip)
            sg_oos = run_drive(oos_days, or_map, br, args.slip)
            outcomes = []
            for day in days:
                oc = or_map.get(day.date)
                if oc is None:
                    continue
                e = drive_entry(day, oc[0], oc[1])
                if e is None:
                    continue
                outcomes.append(resolve(day, e[0], e[1], br, args.slip))
        else:
            if args.daily_coin:
                sg = run_coin_once(days, br, rng, args.slip)
                sg_is = run_coin_once(is_days, br, rng, args.slip)
                sg_oos = run_coin_once(oos_days, br, rng, args.slip)
            else:
                sg = run_coin(days, br, rng, args.samples_per_day, args.slip)
                sg_is = run_coin(is_days, br, rng, args.samples_per_day, args.slip)
                sg_oos = run_coin(oos_days, br, rng, args.samples_per_day, args.slip)
            outcomes = []
            for day in days:
                i = int(day.entry_idx[len(day.entry_idx) // 2])  # fixed mid-window entry
                d = 1 if hash((spec.name, day.date)) % 2 == 0 else -1  # deterministic pseudo-coin
                outcomes.append(resolve(day, i, d, br, args.slip))

        n = sg["win"] + sg["loss"]
        wrate = wr(sg)
        lo, hi = wilson_ci(sg["win"], n)
        edge = wrate - br.baseline
        is_wr = wr(sg_is)
        oos_wr = wr(sg_oos)
        tot = n + sg["unresolved"]
        unres = sg["unresolved"] / tot if tot else 0.0
        max_dd, max_cl = max_drawdown_from_results(days, outcomes, br)

        win = f"{spec.entry_start}-{spec.entry_end}"
        print(
            f"{spec.name:<14} {win:<13} {len(days):>5} {wrate:>5.1%} "
            f"[{lo:>5.1%},{hi:>5.1%}] {edge:>+5.1%} {is_wr:>5.1%} {oos_wr:>5.1%} "
            f"{unres:>4.0%} ${max_dd:>7,.0f} {max_cl:>5}"
        )
        rows.append((spec.name, wrate, edge, is_wr, oos_wr, max_dd, max_cl, len(days)))

    print("\n=== INTERPRETATION ===")
    print("- 'edge' = win rate minus geometric baseline (85.5%). Positive = better than random drift.")
    print("- maxDD$ / maxCL use one deterministic daily path (mid-window entry for coin windows).")
    print("- Multi-sample coin control (--samples-per-day) gives tighter CI; drive uses one trade/day.")
    print("- Globex windows need extended-hours tick data; RTH windows use the 09:30-16:00 cache.")

    best = max((r for r in rows if r[0] != "drive_open"), key=lambda x: x[1], default=None)
    drive = next((r for r in rows if r[0] == "drive_open"), None)
    if best and drive:
        print(f"\nBest non-drive window: {best[0]} at {best[1]:.1%} (drive_open: {drive[1]:.1%})")
        if best[1] > drive[1] + 0.02:
            print("  -> beats drive by >2pp on full sample (check OOS before adopting).")
        elif drive[1] >= best[1]:
            print("  -> drive at open still wins or ties on win rate.")


if __name__ == "__main__":
    main()
