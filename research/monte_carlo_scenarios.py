"""
Monte Carlo scenario compare: recovery-aware nukes, $3.2k vs $4k targets, DLL vs no-DLL.

Fixes the old model bug where retry day used the same $4k bracket after a $1k loss
(credits +$4k from 49k balance = only +$3k net, and uses day-1 win rate).

Usage:
    python research/monte_carlo_scenarios.py
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from research.monte_carlo import (
    DRIVE_PP_DLL,
    DRIVE_PP_NODLL,
    Edge,
    FundedResult,
    Rules,
    _clip,
    _floor,
    simulate_eval,
)

# From research/backtest_nuke_twoday.py (drive, 0 slip) — update after re-run
MEASURED = {
    "4k_recovery": {"p1": 0.240, "p2_cond": 0.162, "p_seq": 0.391},
    "3200_recovery": {"p1": 0.279, "p2_cond": 0.206, "p_seq": 0.451},
    "nodll": {"p1": 0.361, "p2_cond": None, "p_seq": 0.361},
}


@dataclass
class NukeScenario:
    name: str
    nuke_target: float
    recovery_add: float
    has_dll: bool
    mode: str  # "recovery" | "old_bug" | "nodll"
    measured_key: str | None = None


SCENARIOS = [
    NukeScenario("4k OLD MC bug (same bracket retry)", 4_000, 0, True, "old_bug", "4k_recovery"),
    NukeScenario("4k RECOVERY (5k gross day2)", 4_000, 1_000, True, "recovery", "4k_recovery"),
    NukeScenario("3200 RECOVERY (4200 gross day2)", 3_200, 1_000, True, "recovery", "3200_recovery"),
    NukeScenario("no-DLL one shot ($2k risk)", 4_000, 0, False, "nodll", "nodll"),
]


def probs_for(sc: NukeScenario, rules: Rules, use_edge: bool) -> tuple[float, float, float]:
    pp = DRIVE_PP_DLL if sc.has_dll else DRIVE_PP_NODLL
    stop = rules.funded_day_stop
    if sc.mode == "old_bug":
        p1 = MEASURED["4k_recovery"]["p1"]
        return p1, p1, 1 - (1 - p1) ** 2
    if sc.measured_key and sc.measured_key in MEASURED:
        m = MEASURED[sc.measured_key]
        p1 = m["p1"]
        p2 = m["p2_cond"] if m["p2_cond"] is not None else p1
        return p1, p2, m["p_seq"]
    p1_base = stop / (sc.nuke_target + stop)
    p1 = _clip(p1_base + (pp["nuke"] if use_edge else 0))
    if sc.mode == "nodll":
        return p1, 0.0, p1
    day2_t = sc.nuke_target + sc.recovery_add
    p2 = _clip(stop / (day2_t + stop) + (pp["nuke"] if use_edge else 0) * 0.85)
    return p1, p2, 1 - (1 - p1) * (1 - p2)


def simulate_nuke(
    rng: np.random.Generator,
    sc: NukeScenario,
    rules: Rules,
    p1: float,
    p2: float,
) -> tuple[bool, float, int]:
    """Returns (success, equity_delta, days_used)."""
    if sc.mode == "nodll":
        if rng.random() < p1:
            return True, sc.nuke_target, 1
        return False, -rules.funded_day_stop, 1

    if rng.random() < p1:
        return True, sc.nuke_target, 1

    # Day 1 loss (−$1k)
    if sc.mode == "old_bug":
        if rng.random() < p1:  # same WR, same +$4k credit (bug)
            return True, sc.nuke_target, 2
        return False, -2 * rules.funded_day_stop, 2

    # Recovery: day-2 needs gross target + recovery_add
    if rng.random() < p2:
        return True, sc.nuke_target + sc.recovery_add, 2
    return False, -2 * rules.funded_day_stop, 2


def simulate_funded(
    rng: np.random.Generator,
    rules: Rules,
    e_flip: float,
    sc: NukeScenario,
    p1: float,
    p2: float,
) -> FundedResult:
    equity = rules.initial_balance
    peak = equity
    res = FundedResult()
    days = 0

    def busted() -> bool:
        return equity <= _floor(rules.initial_balance, peak, rules.trailing_drawdown)

    while res.payouts < rules.payouts_target and days < rules.funded_horizon_days:
        winning_days = 0

        if res.payouts % 2 == 0:
            hit, delta, n_used = simulate_nuke(rng, sc, rules, p1, p2)
            days += n_used
            equity += delta
            peak = max(peak, equity)
            if hit:
                res.nukes_hit += 1
                winning_days = 1
            else:
                res.busted = True
                return res

        while winning_days < rules.winning_days_required:
            days += 1
            if days >= rules.funded_horizon_days:
                return res
            if rng.random() < e_flip:
                equity += rules.flip_target_dollars
                winning_days += 1
            else:
                equity -= rules.funded_day_stop
            peak = max(peak, equity)
            if busted():
                res.busted = True
                return res

        profit = equity - rules.initial_balance
        if profit <= 0:
            break
        take = min(rules.withdrawal_fraction * profit, rules.payout_cap)
        res.withdrawn += take
        equity -= take
        res.payouts += 1

    return res


def run(args) -> None:
    rng = np.random.default_rng(args.seed)
    nf = args.trials
    bs = args.batch_size

    print("=" * 98)
    print("NUKE SCENARIO COMPARE — recovery-aware vs old MC bug vs $3.2k vs no-DLL")
    print(f"trials={nf:,}  bankroll=${args.bankroll:,.0f}  batch={bs}  rounds={args.rounds}")
    print("=" * 98)

    rows = []
    for sc in SCENARIOS:
        rules = Rules(nuke_target=sc.nuke_target, has_dll=sc.has_dll, payouts_target=4)
        if not sc.has_dll:
            rules = replace(rules, eval_cost=95.0)

        pp = DRIVE_PP_DLL if sc.has_dll else DRIVE_PP_NODLL
        e_eval = _clip(
            rules.eval_stop_pts / (rules.eval_target_pts + rules.eval_stop_pts) + pp["eval"])
        e_flip = _clip(rules.dll / (rules.flip_target_dollars + rules.dll) + pp["flip"])
        p1, p2, p_seq = probs_for(sc, rules, use_edge=True)

        fr = [simulate_funded(rng, rules, e_flip, sc, p1, p2) for _ in range(nf)]
        w = np.array([x.withdrawn for x in fr])
        payouts = np.array([x.payouts for x in fr])
        nukes = np.array([x.nukes_hit for x in fr])

        nets = []
        for _ in range(args.batches * bs):
            net = -rules.eval_cost
            if simulate_eval(rng, rules, Edge(eval_win=e_eval)):
                net += simulate_funded(rng, rules, e_flip, sc, p1, p2).withdrawn
            nets.append(net)
        nets = np.array(nets).reshape(args.batches, bs).sum(axis=1)

        finals, ruins = [], 0
        for _ in range(args.campaign_trials):
            bankroll = args.bankroll
            ruined = False
            for _ in range(args.rounds):
                cost = rules.eval_cost * bs
                if bankroll < cost:
                    ruined = True
                    break
                bankroll -= cost
                for _ in range(bs):
                    if simulate_eval(rng, rules, Edge(eval_win=e_eval)):
                        bankroll += simulate_funded(rng, rules, e_flip, sc, p1, p2).withdrawn
            finals.append(bankroll)
            ruins += int(ruined)

        pre_p1 = sc.nuke_target + 4 * rules.flip_target_dollars
        take_p1 = min(pre_p1 * rules.withdrawal_fraction, rules.payout_cap)

        print(f"\n--- {sc.name} ---")
        print(f"  p_day1={p1:.1%}  p_day2={p2:.1%}  measured 2-day seq={p_seq:.1%}")
        print(f"  1-(1-p1)(1-p2) = {1-(1-p1)*(1-p2):.1%}  |  1st nuke landed {np.mean(nukes>=1):.1%}")
        print(f"  Pre-P1 profit (nuke+4 flips): ${pre_p1:,.0f}  -> 50% withdraw ~${take_p1:,.0f}")
        print(f"  FUNDED: mean withdrawn ${w.mean():,.0f} | mean payouts {payouts.mean():.2f} | "
              f"P(bust) {np.mean([x.busted for x in fr]):.1%}")
        print("  reach payout: " + "  ".join(
            f"P{k} {np.mean(payouts >= k):.0%}" for k in range(1, 5)))
        print(f"  BATCH: mean ${nets.mean():+,.0f} ROI {nets.mean()/(rules.eval_cost*bs):+.0%} | "
              f"P(profit) {np.mean(nets>0):.1%}")
        print(f"  CAMPAIGN: P(ruin) {ruins/args.campaign_trials:.1%} | "
              f"median ${np.median(finals):+,.0f} | P(end>start) {np.mean(np.array(finals)>args.bankroll):.1%}")

        rows.append((sc.name, w.mean(), payouts.mean(), np.mean(nukes >= 1), take_p1, nets.mean()))

    print("\n" + "=" * 98)
    print(f"{'scenario':<38} {'$/acct':>8} {'pyts':>5} {'1stNk':>6} {'P1 wdr':>8} {'batch$':>10}")
    print("-" * 98)
    for r in rows:
        print(f"{r[0]:<38} ${r[1]:>7,.0f} {r[2]:>5.2f} {r[3]:>5.1%} ${r[4]:>7,.0f} ${r[5]:>+9,.0f}")
    print("=" * 98)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--trials", type=int, default=20_000)
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--batches", type=int, default=500)
    p.add_argument("--bankroll", type=float, default=5_000.0)
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--campaign-trials", type=int, default=2_000)
    p.add_argument("--seed", type=int, default=7)
    run(p.parse_args())


if __name__ == "__main__":
    main()
