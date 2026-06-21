"""Compare 50k vs 150k brackets: win rate, UNRESOLVED rate, CI, drive edge.

Win rate is contract-agnostic (points-based); what changes with a bigger
absolute target is the UNRESOLVED rate (can't reach the target before the
session ends). That is the hidden cost of a bigger account / bigger nuke.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.backtest_common import (
    Bracket, collect_drive_entries, load_days, run_drive, run_coinflip, wilson_ci,
)
import numpy as np

def line(label, br, entries, rng):
    sg = run_drive(entries, br)
    n = sg["win"] + sg["loss"]
    tot = n + sg["unresolved"]
    wr = sg["win"]/n if n else 0
    unres = sg["unresolved"]/tot if tot else 0
    lo, hi = wilson_ci(sg["win"], n)
    cf = run_coinflip(days, br, 30, rng)
    cfn = cf["win"]+cf["loss"]
    cfwr = cf["win"]/cfn if cfn else 0
    print(f"{label:<34} {br.target_pts:>6.1f}/{br.stop_pts:<4.0f} {br.baseline:>6.1%} "
          f"{cfwr:>7.1%} {wr:>7.1%} [{lo:>5.1%},{hi:>5.1%}] {wr-br.baseline:>+6.1%} {unres:>6.1%}")

days = load_days()
entries = collect_drive_entries(days)
rng = np.random.default_rng(1)
print(f"Days: {len(days)} | drive entries: {len(entries)}\n")
print(f"{'bracket':<34} {'tgt/stp':>11} {'base':>6} {'coin':>7} {'drive':>7} {'  CI':>15} {'edge':>6} {'unres':>6}")
print("-"*104)

print("--- 50K  (2 mini funded, $40/pt, $1k DLL=25pt) ---")
line("50k nuke $3,200", Bracket("a",80,25), entries, rng)
line("50k recov $4,200 (day2, 25pt)", Bracket("a",105,25), entries, rng)
line("50k flip $170 (1mini 8.5/50)", Bracket("a",8.5,50), entries, rng)

print("\n--- 150K  (3 mini funded, $60/pt, $3k DLL=50pt, $4.5k trail) ---")
line("150k nuke $5,000", Bracket("b",83.33,50), entries, rng)
line("150k nuke $6,000", Bracket("b",100,50), entries, rng)
line("150k nuke $7,000", Bracket("b",116.67,50), entries, rng)
line("150k nuke $9,000", Bracket("b",150,50), entries, rng)
line("150k nuke $10,000", Bracket("b",166.67,50), entries, rng)
line("150k flip $150 (3mini 2.5/50)", Bracket("b",2.5,50), entries, rng)
# day-2 recovery on 150k: after $3k loss, only $1.5k room => 25pt stop
print("  recovery (day2 risks $1.5k = 25pt stop):")
line("150k recov $5k->$8k gross", Bracket("b",133.33,25), entries, rng)
line("150k recov $7k->$10k gross", Bracket("b",166.67,25), entries, rng)
line("150k recov $9k->$12k gross", Bracket("b",200,25), entries, rng)
