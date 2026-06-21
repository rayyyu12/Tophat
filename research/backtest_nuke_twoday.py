"""
Direct two-day NUKE sequence backtest (DLL second-chance with recovery target).

Models the real Topstep path:
  Day 1: bracket for the base nuke target (e.g. 100pt / 25pt = $4k / $1k).
  Day 1 loss: equity −$1,000; account survives if above trailing floor.
  Day 2: WIDER target to recover the loss (e.g. 125pt = $5k gross, $4k net).
  Day 2 loss: account busted (−$2,000 total from start).

Compares measured P(2-day success) to the independence shortcut
  1 − (1 − p₁)(1 − p₂)  using single-day win rates (the old MC assumption).

Pre-registered brackets (2 minis, $40/pt):
  $4,000 nuke : 100 / 25  |  retry $5,000 : 125 / 25
  $3,200 nuke :  80 / 25  |  retry $4,200 : 105 / 25
  no-DLL       : 100 / 50  |  one shot only ($2k risk)

Usage:
    python research/backtest_nuke_twoday.py
    python research/backtest_nuke_twoday.py --slip 0.25
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.backtest_common import (
    Bracket,
    CACHE_FILE,
    collect_drive_entries,
    load_days,
    resolve,
    wilson_ci,
)

# 2 minis × $20/pt = $40/pt
BR_4K = Bracket("nuke_4k", 100.0, 25.0)
BR_5K_RETRY = Bracket("retry_5k", 125.0, 25.0)
BR_3200 = Bracket("nuke_3200", 80.0, 25.0)
BR_4200_RETRY = Bracket("retry_4200", 105.0, 25.0)
BR_NODLL = Bracket("nuke_nodll", 100.0, 50.0)

INITIAL = 50_000.0
TRAILING = 2_000.0
DLL = 1_000.0


@dataclass(frozen=True)
class NukePlan:
    name: str
    day1: Bracket
    day2: Bracket | None  # None = one-shot (no-DLL)
    day1_dollars: float
    day2_dollars: float | None


PLANS = [
    NukePlan("4k + recovery", BR_4K, BR_5K_RETRY, 4_000.0, 5_000.0),
    NukePlan("3200 + recovery", BR_3200, BR_4200_RETRY, 3_200.0, 4_200.0),
    NukePlan("no-DLL one shot", BR_NODLL, None, 4_000.0, None),
]


@dataclass
class SeqResult:
    outcome: str  # win_d1 | win_d2 | bust_d2 | bust_nodll | skip
    day1: str | None = None
    day2: str | None = None


def _floor(peak: float) -> float:
    return min(INITIAL, peak - TRAILING)


def run_single(entries, br: Bracket, slip: float) -> dict:
    res = []
    for day, i, d in entries:
        res.append(resolve(day, i, d, br, slip))
    n = sum(1 for r in res if r in ("win", "loss"))
    wins = sum(1 for r in res if r == "win")
    return {"win": wins, "loss": sum(1 for r in res if r == "loss"),
            "unresolved": sum(1 for r in res if r == "unresolved"), "n": n,
            "rate": wins / n if n else 0.0}


def run_twoday(entries, plan: NukePlan, slip: float) -> list[SeqResult]:
    """Pair consecutive drive-entry days into 2-day nuke cycles."""
    out: list[SeqResult] = []
    i = 0
    while i < len(entries):
        d1, e1, dir1 = entries[i]
        if plan.day2 is None:
            r1 = resolve(d1, e1, dir1, plan.day1, slip)
            if r1 == "win":
                out.append(SeqResult("win_d1", r1))
            elif r1 == "loss":
                out.append(SeqResult("bust_nodll", r1))
            else:
                out.append(SeqResult("skip", r1))
            i += 1
            continue

        if i + 1 >= len(entries):
            break
        d2, e2, dir2 = entries[i + 1]
        r1 = resolve(d1, e1, dir1, plan.day1, slip)
        if r1 == "win":
            out.append(SeqResult("win_d1", r1))
            i += 1
            continue
        if r1 == "unresolved":
            out.append(SeqResult("skip", r1))
            i += 1
            continue

        # Day 1 loss: −$1k; survive if above floor (peak still 50k → floor 48k)
        equity = INITIAL - DLL
        if equity <= _floor(INITIAL):
            out.append(SeqResult("bust_d2", r1, None))
            i += 2
            continue

        r2 = resolve(d2, e2, dir2, plan.day2, slip)
        if r2 == "win":
            out.append(SeqResult("win_d2", r1, r2))
        elif r2 == "loss":
            out.append(SeqResult("bust_d2", r1, r2))
        else:
            out.append(SeqResult("skip", r1, r2))
        i += 2  # consumed a 2-day cycle
    return out


def summarize(seqs: list[SeqResult]) -> dict:
    usable = [s for s in seqs if s.outcome != "skip"]
    wins = [s for s in usable if s.outcome in ("win_d1", "win_d2")]
    d1_wins = sum(1 for s in usable if s.outcome == "win_d1")
    d2_wins = sum(1 for s in usable if s.outcome == "win_d2")
    busts = sum(1 for s in usable if s.outcome.startswith("bust"))
    n = len(usable)
    rate = len(wins) / n if n else 0.0
    lo, hi = wilson_ci(len(wins), n)
    return {
        "n": n, "wins": len(wins), "rate": rate, "ci": (lo, hi),
        "d1_wins": d1_wins, "d2_wins": d2_wins, "busts": busts,
        "skipped": len(seqs) - n,
    }


def conditional_d2_rate(seqs: list[SeqResult]) -> float | None:
    """P(win day 2 | day 1 was a loss and day 2 was attempted)."""
    d2_trials = [s for s in seqs if s.day1 == "loss" and s.day2 is not None]
    if not d2_trials:
        return None
    d2_wins = sum(1 for s in d2_trials if s.outcome == "win_d2")
    return d2_wins / len(d2_trials)


def main() -> None:
    ap = argparse.ArgumentParser(description="Two-day NUKE sequence backtest")
    ap.add_argument("--slip", type=float, default=0.0)
    ap.add_argument("--cache", default=CACHE_FILE)
    args = ap.parse_args()

    days = load_days(args.cache)
    entries = collect_drive_entries(days)
    mid = len(entries) // 2
    is_e = entries[:mid]
    oos_e = entries[mid:]

    print("=== TWO-DAY NUKE SEQUENCE BACKTEST (drive, consecutive days) ===")
    print(f"Days: {len(days)} | Drive entries: {len(entries)}")
    print(f"Trailing ${TRAILING:,.0f} | DLL ${DLL:,.0f} | slip {args.slip} pt\n")

    for plan in PLANS:
        sg = run_twoday(entries, plan, args.slip)
        sg_is = run_twoday(is_e, plan, args.slip)
        sg_oos = run_twoday(oos_e, plan, args.slip)
        sm = summarize(sg)
        sm_is = summarize(sg_is)
        sm_oos = summarize(sg_oos)

        p1 = run_single(entries, plan.day1, args.slip)["rate"]
        p2_br = plan.day2 or plan.day1
        p2_single = run_single(entries, p2_br, args.slip)["rate"]
        p2_cond = conditional_d2_rate(sg)
        indep = 1.0 - (1.0 - p1) * (1.0 - p2_single) if plan.day2 else p1
        indep_cond = (1.0 - (1.0 - p1) * (1.0 - p2_cond)) if p2_cond is not None else None

        print(f"--- {plan.name} ---")
        print(f"  Day1 bracket: {plan.day1.target_pts}/{plan.day1.stop_pts} pt "
              f"(${plan.day1_dollars:,.0f})  single-day WR {p1:.1%}  "
              f"(baseline {plan.day1.baseline:.1%})")
        if plan.day2:
            print(f"  Day2 bracket: {plan.day2.target_pts}/{plan.day2.stop_pts} pt "
                  f"(${plan.day2_dollars:,.0f} gross)  single-day WR {p2_single:.1%}  "
                  f"(baseline {plan.day2.baseline:.1%})")
            if p2_cond is not None:
                print(f"  Day2 WR | day1 loss: {p2_cond:.1%}  (conditional, same sample)")
        print(f"  2-day measured : {sm['rate']:.1%}  [{sm['ci'][0]:.1%},{sm['ci'][1]:.1%}]  "
              f"n={sm['n']}  (d1={sm['d1_wins']}  d2={sm['d2_wins']}  bust={sm['busts']}  "
              f"skip={sm['skipped']})")
        print(f"  IS / OOS       : {sm_is['rate']:.1%} / {sm_oos['rate']:.1%}")
        print(f"  Independence   : 1-(1-p1)(1-p2_single) = {indep:.1%}", end="")
        if indep_cond is not None:
            delta = sm["rate"] - indep_cond
            print(f"  | 1-(1-p1)(1-p2|loss) = {indep_cond:.1%}  "
                  f"(delta measured {delta:+.1%})")
        else:
            print()
        print()


if __name__ == "__main__":
    main()
