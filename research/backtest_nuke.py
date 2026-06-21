"""
Drive-momentum backtest — NUKE bracket only (50K with DLL).

Signal: at 09:45 ET, enter in the direction of the 09:30-09:45 opening move.
Fill: next 1s bar open (same as research/backtest.py).

Bracket matches live engine + backtest nukeDLL row (2 minis):
  target 100 pts ($4,000)  |  stop 25 pts ($1,000 DLL)  |  baseline ~20%

Usage:
    python research/backtest_nuke.py
    python research/backtest_nuke.py --monthly --slip 0.25
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from research.backtest_common import (
    Bracket,
    CACHE_FILE,
    ENTRY_END,
    ENTRY_START,
    collect_drive_entries,
    edge_str,
    fmt,
    load_days,
    monthly_breakdown,
    run_coinflip,
    run_drive,
)

# Live engine: nuke_target_dollars=4000, dll=1000, funded_contracts=2, point_value=20
NUKE_DLL = Bracket("nukeDLL", 100.0, 25.0)
NUKE_NODLL = Bracket("nukeNoDLL", 100.0, 50.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="TopHat drive backtest — NUKE")
    ap.add_argument("--samples-per-day", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--slip", type=float, default=0.0,
                    help="adverse entry slippage in points")
    ap.add_argument("--monthly", action="store_true")
    ap.add_argument("--no-dll", action="store_true",
                    help="use 100/50 (2:1) bracket instead of DLL 100/25 (1:4)")
    ap.add_argument("--cache", default=CACHE_FILE)
    args = ap.parse_args()

    br = NUKE_NODLL if args.no_dll else NUKE_DLL
    rng = np.random.default_rng(args.seed)
    days = load_days(args.cache)
    mid = len(days) // 2
    entries = collect_drive_entries(days)
    entries_is = collect_drive_entries(days[:mid])
    entries_oos = collect_drive_entries(days[mid:])

    print(f"=== TopHat DRIVE — {br.name.upper()} ===")
    print(f"Bracket: target {br.target_pts} pts / stop {br.stop_pts} pts"
          f"  (baseline {br.baseline:.1%})")
    print(f"Loaded {len(days)} days ({days[0].date} .. {days[-1].date})")
    print(f"Drive fired on {len(entries)}/{len(days)} days "
          f"({len(entries)/len(days):.0%})")
    print(f"Entry window {ENTRY_START}-{ENTRY_END} ET | slip {args.slip} pts\n")

    cf = run_coinflip(days, br, args.samples_per_day, rng, args.slip)
    sg = run_drive(entries, br, args.slip)
    sg_is = run_drive(entries_is, br, args.slip)
    sg_oos = run_drive(entries_oos, br, args.slip)

    print(f"  {'coinflip control':<16} : {fmt(cf)}")
    print(f"  {'drive (mom)':<16} : {fmt(sg)}   edge {edge_str(sg, br)}")
    print(f"  {'  IS / OOS':<16} : IS {fmt(sg_is)} | OOS {fmt(sg_oos)}")

    if args.monthly:
        print("\n=== drive win rate by month ===")
        mb = monthly_breakdown(entries, br, args.slip)
        for m in sorted(mb):
            w, n = mb[m]
            print(f"  {m} : {w/n:>6.1%}  ({w}/{n})")


if __name__ == "__main__":
    main()
