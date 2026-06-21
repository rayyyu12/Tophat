"""
Full-pipeline backtest: run the strategy ENGINE over real sequential NQ days.

This wires engine.py (the broker-agnostic brain) to a BacktestBroker built from
backtest.py's next-bar-fill `resolve`. Each simulated account walks forward through
actual trading days one trade per day: the engine picks the phase/lifecycle bracket,
the day's drive-momentum move picks the direction, and `resolve` settles win/loss
against that day's 1-second bars. We then aggregate eval pass %, funded withdrawals,
payouts, and bust rates.

Why this matters (vs monte_carlo.py): the Monte Carlo draws each day's outcome from
a fixed measured probability (independent draws). This pipeline uses the ACTUAL,
path-dependent, autocorrelated sequence of historical days. In particular the funded
nuke re-tries on consecutive REAL days, so this is a direct test of the "second
chance" independence assumption that the Monte Carlo only models as 1-(1-p)^2.

Limitation: with ~251 days, accounts reuse the day series (random start offset, wrap
around), so accounts overlap and outcomes are not fully independent. Treat the
numbers as a cross-check on the Monte Carlo, not a second independent universe.

Usage:
    python pipeline.py
    python pipeline.py --accounts 20000 --slip 0.25
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

import tophat.engine as e
from research.backtest import CACHE_FILE, Bracket, drive_entry, load_days, resolve

EVAL_COST = 85.0          # 50K with-DLL ticket
EVAL_MAX_DAYS = 30        # abandon an eval that neither passes nor busts in 30 days
STEP_CAP = 500            # hard safety on the per-account day loop


@dataclass
class Outcome:
    passed: bool = False
    withdrawn: float = 0.0
    payouts: int = 0
    nukes_hit: int = 0
    busted: bool = False


def day_direction(day, mode: str, rng) -> tuple[int, int | None]:
    """Return (direction, entry_bar_index). direction 0 => no trade today."""
    if mode == "coin":
        return (1 if rng.random() < 0.5 else -1), int(day.entry_idx[0])
    de = drive_entry(day)
    if de is None:
        return 0, None
    return de[1], de[0]


def run_account(days, start: int, cfg: e.AccountConfig, mode: str,
                rng, slip: float) -> Outcome:
    s = e.AccountState()
    n = len(days)
    out = Outcome()
    i = start
    for _ in range(STEP_CAP):
        day = days[i % n]
        i += 1
        e.start_new_day(s)
        direction, entry_i = day_direction(day, mode, rng)
        dec = e.decide(cfg, s, direction)

        if dec.action == e.Action.RETIRE:
            break
        if dec.action == e.Action.MANUAL:        # equity hit the trailing floor
            out.busted = True
            break
        if dec.action == e.Action.HOLD:          # flat open or DLL lockout
            continue

        plan = dec.plan
        br = Bracket(plan.label, plan.target_pts, plan.stop_pts)
        r = resolve(day, entry_i, plan.direction, br, slip)
        if r == "unresolved":                    # never reached target/stop by 16:00
            continue
        won = r == "win"

        if s.phase == e.Phase.EVAL:
            e.on_eval_result(cfg, s, won)
            if s.phase == e.Phase.FUNDED:
                out.passed = True
            elif s.days_traded >= EVAL_MAX_DAYS:  # abandon a stalled eval
                break
        else:
            if plan.label in ("nuke", "renuke") and won:
                out.nukes_hit += 1
            out.withdrawn += e.on_funded_result(cfg, s, plan, won)

    out.payouts = s.payouts_taken
    return out


def run_cohort(days, cfg, mode, n_accounts, rng, slip) -> list[Outcome]:
    starts = rng.integers(0, len(days), size=n_accounts)
    return [run_account(days, int(st), cfg, mode, rng, slip) for st in starts]


def summarize(tag: str, outs: list[Outcome], payouts_target: int) -> dict:
    passed = np.array([o.passed for o in outs])
    withdrawn = np.array([o.withdrawn for o in outs])
    payouts = np.array([o.payouts for o in outs])
    nukes = np.array([o.nukes_hit for o in outs])

    funded = withdrawn[passed]
    fp = payouts[passed]
    net = withdrawn - EVAL_COST
    print(f"\n--- {tag} ---")
    print(f"  EVAL  pass {passed.mean():.1%}")
    print(f"  FUNDED/acct (of passers): mean withdrawn ${funded.mean() if funded.size else 0:,.0f}"
          f" | mean payouts {fp.mean() if fp.size else 0:.2f}")
    if fp.size:
        print("    reach payout: " + "  ".join(
            f"P{k} {np.mean(fp >= k):.0%}" for k in range(1, payouts_target + 1)))
        print(f"    1st nuke landed {np.mean(nukes[passed] >= 1):.0%}"
              f" | re-nuke landed {np.mean(nukes[passed] >= 2):.0%}")
    print(f"  PURCHASED/acct: mean withdrawn ${withdrawn.mean():,.0f}"
          f" | net ${net.mean():+,.0f} | ROI {net.mean()/EVAL_COST:+.0%}")
    return {
        "tag": tag, "pass": passed.mean(),
        "funded_$": funded.mean() if funded.size else 0.0,
        "payouts": fp.mean() if fp.size else 0.0,
        "net": net.mean(), "roi": net.mean() / EVAL_COST,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="TopHat full-pipeline engine backtest")
    ap.add_argument("--accounts", type=int, default=20_000)
    ap.add_argument("--slip", type=float, default=0.0,
                    help="adverse entry slippage in points (0.25 = 1 tick)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    days = load_days(CACHE_FILE)
    cfg = e.AccountConfig()

    print("=" * 78)
    print("TOPHAT 50k DLL  |  full pipeline over real sequential days")
    print(f"days {len(days)} ({days[0].date}..{days[-1].date}) | "
          f"{args.accounts:,} accts/cohort | slip {args.slip} pts")
    print("lifecycle: nuke+4flips, 5flips, RE-NUKE+4flips, 5flips (nuke on cycles 1&3)")
    print("=" * 78)

    rows = []
    for mode in ("coin", "drive"):
        outs = run_cohort(days, cfg, mode, args.accounts, rng, args.slip)
        rows.append(summarize(f"{mode}", outs, cfg.payouts_target))

    print("\n" + "=" * 78)
    print(f"{'mode':>6} | {'pass':>6} {'$/funded':>9} {'payouts':>7} | "
          f"{'net/acct':>9} {'ROI':>7}")
    print("-" * 78)
    for r in rows:
        print(f"{r['tag']:>6} | {r['pass']:>6.1%} ${r['funded_$']:>8,.0f} "
              f"{r['payouts']:>7.2f} | ${r['net']:>+8,.0f} {r['roi']:>+7.0%}")
    print("=" * 78)


if __name__ == "__main__":
    main()
