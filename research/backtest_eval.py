"""
Eval bracket win rates (drive) for path-aware Monte Carlo.

Pre-registered brackets (stop 9.5 pt = ~$950 at 5 minis / use 25 pt for 2 minis $1k DLL).
Target pts scaled for partial pass days (consistency cap $1,500/day on $3k goal).

Usage:
    python research/backtest_eval.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.backtest_common import Bracket, collect_drive_entries, edge_str, fmt, load_days, run_drive

# Pre-registered eval brackets (points; win rate is contract-agnostic in bar-resolution)
EVAL_BRACKETS_5M = [
    ("full_1550", 15.5, 9.5, 1550.0),
    ("tgt_1500", 15.0, 9.5, 1500.0),
    ("tgt_1000", 10.0, 9.5, 1000.0),
    ("tgt_750", 7.5, 9.5, 750.0),
    ("tgt_500", 5.0, 9.5, 500.0),
]

EVAL_BRACKETS_2M = [
    ("full_1550", 38.75, 25.0, 1550.0),
    ("tgt_1500", 37.5, 25.0, 1500.0),
    ("tgt_1000", 25.0, 25.0, 1000.0),
    ("tgt_750", 18.75, 25.0, 750.0),
    ("tgt_500", 12.5, 25.0, 500.0),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slip", type=float, default=0.0)
    args = ap.parse_args()

    days = load_days()
    entries = collect_drive_entries(days)

    print("=== EVAL DRIVE WIN RATES BY TARGET (for path-aware MC) ===")
    print(f"Days: {len(days)} | Drive entries: {len(entries)} | slip {args.slip}\n")

    for label, brackets in [("5-minis equiv (9.5pt stop)", EVAL_BRACKETS_5M),
                            ("2-minis equiv (25pt stop, $1k DLL)", EVAL_BRACKETS_2M)]:
        print(f"--- {label} ---")
        print(f"{'name':<12} {'tgt/stp':>12} {'$tgt':>6} {'base':>6} {'drive':>6} {'edge':>6}")
        for name, tgt, stp, dollars in brackets:
            br = Bracket(name, tgt, stp)
            sg = run_drive(entries, br, args.slip)
            n = sg["win"] + sg["loss"]
            wr = sg["win"] / n if n else 0.0
            print(f"{name:<12} {tgt:>5.1f}/{stp:<5.1f} ${dollars:>5.0f} "
                  f"{br.baseline:>5.1%} {wr:>5.1%} {edge_str(sg, br):>6}")
        print()


if __name__ == "__main__":
    main()
