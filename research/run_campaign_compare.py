"""
Path-aware eval simulation + campaign compare ($4k vs $3.2k nuke).

Eval model (vs old fixed +$1500/-$1000 coin flip):
  - $3,000 pass target, min 2 trading days
  - $1,000 DLL daily loss cap (loss day = -$1,000)
  - Consistency: max $1,500 counted win per day (50% of goal)
  - Dynamic bracket: target = min(remaining profit, $1,500)
  - Win probability from measured drive rates per bracket (backtest_eval.py)
  - Trailing $2,000 EOD drawdown

Monte Carlo batching is AGNOSTIC to copy-trading: each account is an independent
Bernoulli/path draw. Buying 10 tickets in a round != trading them simultaneously.
Pass rate per account does not change whether you trade 2/day or 10/day.

Usage:
    python research/run_campaign_compare.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from research.backtest_common import Bracket, collect_drive_entries, load_days, resolve
from research.monte_carlo import DRIVE_PP_DLL, Rules, _clip, _floor
from research.monte_carlo_scenarios import (
    MEASURED,
    NukeScenario,
    probs_for,
    simulate_funded,
    simulate_nuke,
)

# Measured drive WR by (target_pts, stop_pts) — from backtest_eval.py, 0 slip
# 2-minis brackets (25 pt stop = $1,000 DLL)
EVAL_WR_2M: dict[tuple[float, float], float] = {}
EVAL_BRACKETS_2M = [
    (38.75, 25.0, 1550.0),
    (37.5, 25.0, 1500.0),
    (25.0, 25.0, 1000.0),
    (18.75, 25.0, 750.0),
    (12.5, 25.0, 500.0),
]

# 5-minis brackets (9.5 pt stop) — what backtest.py / engine default uses
EVAL_WR_5M: dict[tuple[float, float], float] = {}
EVAL_BRACKETS_5M = [
    (15.5, 9.5, 1550.0),
    (15.0, 9.5, 1500.0),
    (10.0, 9.5, 1000.0),
    (7.5, 9.5, 750.0),
    (5.0, 9.5, 500.0),
]

PASS_PROFIT = 3_000.0
CONSISTENCY_CAP = 1_500.0
DLL = 1_000.0
INITIAL = 50_000.0
TRAILING = 2_000.0
EVAL_MAX_DAYS = 30
PT_VALUE_2M = 40.0   # 2 minis × $20
PT_VALUE_5M = 100.0  # 5 minis × $20

BS = 10
ROUNDS = 10
BANK = 5_000.0
CAMPAIGN_TRIALS = 5_000
NF = 30_000
SEED = 42


def _fill_wr_cache(entries, brackets, pt_val, cache: dict) -> None:
    for tgt, stp, _ in brackets:
        br = Bracket("e", tgt, stp)
        wins = losses = 0
        for day, i, d in entries:
            r = resolve(day, i, d, br, 0.0)
            if r == "win":
                wins += 1
            elif r == "loss":
                losses += 1
        n = wins + losses
        cache[(tgt, stp)] = wins / n if n else br.baseline


def bracket_for_remaining(remaining: float, pt_val: float,
                          brackets: list) -> tuple[float, float, float]:
    """Return (target_pts, stop_pts, win_dollars) for today's trade."""
    daily = min(remaining, CONSISTENCY_CAP)
    daily = max(daily, 50.0)  # minimum meaningful day
    best = brackets[0]
    for tgt, stp, nominal in brackets:
        if nominal >= daily - 0.01 or tgt == brackets[-1][0]:
            # pick closest bracket at or above daily need
            if nominal >= daily - 0.01:
                best = (tgt, stp, min(daily, nominal))
                break
            best = (tgt, stp, daily)
    tgt, stp, _ = best
    # scale target pts to exact dollar need when between rungs
    exact_tgt = daily / pt_val
    return exact_tgt, stp, daily


def wr_lookup(tgt: float, stp: float, cache: dict, brackets) -> float:
    """Nearest pre-registered bracket WR."""
    best_key = min(cache.keys(), key=lambda k: abs(k[0] - tgt) + abs(k[1] - stp) * 0.01)
    return cache[best_key]


def simulate_eval_simple(rng, p_win: float, rules: Rules) -> bool:
    """Old MC: fixed +$1500 / -$1000 each day."""
    eq = rules.initial_balance
    peak = eq
    days = 0
    while days < rules.eval_max_days:
        days += 1
        if rng.random() < p_win:
            eq += rules.eval_win_dollars
        else:
            eq -= rules.eval_loss_dollars
        peak = max(peak, eq)
        if eq <= _floor(rules.initial_balance, peak, rules.trailing_drawdown):
            return False
        if eq - rules.initial_balance >= rules.eval_pass_profit and days >= rules.eval_min_days:
            return True
    return False


def simulate_eval_path(rng, cache: dict, brackets, pt_val: float,
                       wr_table: dict | None = None) -> bool:
    """Dynamic-target eval with bracket-specific win rates."""
    eq = INITIAL
    peak = eq
    days = 0
    table = wr_table or cache
    while days < EVAL_MAX_DAYS:
        remaining = PASS_PROFIT - (eq - INITIAL)
        if remaining <= 0 and days >= 2:
            return True
        tgt, stp, win_d = bracket_for_remaining(remaining, pt_val, brackets)
        p = wr_lookup(tgt, stp, table, brackets)
        days += 1
        if rng.random() < p:
            eq += min(win_d, CONSISTENCY_CAP)
        else:
            eq -= DLL
        peak = max(peak, eq)
        if eq <= _floor(INITIAL, peak, TRAILING):
            return False
    return False


def simulate_eval_historical(entries, start: int, cache: dict, brackets,
                             pt_val: float) -> bool:
    """Walk real consecutive days with dynamic brackets."""
    eq = INITIAL
    peak = eq
    days = 0
    n = len(entries)
    i = start
    while days < EVAL_MAX_DAYS:
        remaining = PASS_PROFIT - (eq - INITIAL)
        if remaining <= 0 and days >= 2:
            return True
        day, ei, d = entries[i % n]
        i += 1
        tgt, stp, win_d = bracket_for_remaining(remaining, pt_val, brackets)
        br = Bracket("e", tgt, stp)
        r = resolve(day, ei, d, br, 0.0)
        if r == "unresolved":
            continue
        days += 1
        if r == "win":
            eq += min(win_d, CONSISTENCY_CAP)
        else:
            eq -= DLL
        peak = max(peak, eq)
        if eq <= _floor(INITIAL, peak, TRAILING):
            return False
    return False


def run_campaign(rng, sc: NukeScenario, p1, p2, e_flip, eval_fn, rules) -> dict:
    batch_cost = rules.eval_cost * BS
    finals, profits, batch_nets = [], [], []
    ruins = 0
    for _ in range(CAMPAIGN_TRIALS):
        bankroll = BANK
        ruined = False
        for _ in range(ROUNDS):
            if bankroll < batch_cost:
                ruined = True
                break
            bankroll -= batch_cost
            batch_w = 0.0
            for _ in range(BS):
                acct = -rules.eval_cost
                if eval_fn(rng):
                    acct += simulate_funded(rng, rules, e_flip, sc, p1, p2).withdrawn
                batch_w += acct
            bankroll += batch_w
            batch_nets.append(batch_w)
        finals.append(bankroll)
        profits.append(bankroll - BANK)
        ruins += int(ruined)
    return {
        "ruin": ruins / CAMPAIGN_TRIALS,
        "mean_final": float(np.mean(finals)),
        "median_final": float(np.median(finals)),
        "mean_profit": float(np.mean(profits)),
        "p_ahead": float(np.mean(np.array(finals) > BANK)),
        "batch_mean": float(np.mean(batch_nets)),
        "batch_p_prof": float(np.mean(np.array(batch_nets) > 0)),
    }


def funded_stats(rng, sc, p_day1, p_day2, e_flip, eval_fn, rules) -> dict:
    passes = payout1 = nland = 0
    for _ in range(NF):
        if eval_fn(rng):
            passes += 1
            fr = simulate_funded(rng, rules, e_flip, sc, p_day1, p_day2)
            if fr.payouts >= 1:
                payout1 += 1
            if fr.nukes_hit >= 1:
                nland += 1
    return {
        "pass": passes / NF,
        "p_payout1": payout1 / NF,
        "p_payout1_cond": payout1 / passes if passes else 0.0,
        "p_nuke": nland / NF,
        "p_nuke_cond": nland / passes if passes else 0.0,
    }


def main() -> None:
    rng = np.random.default_rng(SEED)
    days = load_days()
    entries = collect_drive_entries(days)

    cache_2m: dict = {}
    cache_5m: dict = {}
    _fill_wr_cache(entries, EVAL_BRACKETS_2M, PT_VALUE_2M, cache_2m)
    _fill_wr_cache(entries, EVAL_BRACKETS_5M, PT_VALUE_5M, cache_5m)

    pp = DRIVE_PP_DLL
    rules = Rules(payouts_target=4)
    e_flip = _clip(rules.dll / (rules.flip_target_dollars + rules.dll) + pp["flip"])
    e_simple = _clip(rules.eval_stop_pts / (rules.eval_target_pts + rules.eval_stop_pts) + pp["eval"])

    sc4 = NukeScenario("4k", 4_000, 1_000, True, "recovery", "4k_recovery")
    sc32 = NukeScenario("3200", 3_200, 1_000, True, "recovery", "3200_recovery")
    p1_4, p2_4, _ = probs_for(sc4, rules, True)
    p1_32, p2_32, _ = probs_for(sc32, rules, True)

    print("=" * 78)
    print("EVAL WIN RATES (drive, measured on historical days)")
    print("=" * 78)
    print("\n5 minis (9.5 pt stop) — current backtest.py / engine default:")
    for tgt, stp, usd in EVAL_BRACKETS_5M:
        print(f"  ${usd:>5.0f} day  {tgt:>5.2f}/{stp} pt  ->  {cache_5m[(tgt, stp)]:.1%}")
    print("\n2 minis (25 pt stop = $1,000 DLL) — your proposed sizing:")
    for tgt, stp, usd in EVAL_BRACKETS_2M:
        print(f"  ${usd:>5.0f} day  {tgt:>5.2f}/{stp} pt  ->  {cache_2m[(tgt, stp)]:.1%}")

    print("\n" + "=" * 78)
    print("EVAL PASS RATE COMPARISON")
    print("=" * 78)
    models = [
        ("Old MC (fixed +$1500/-$1000, 45.6% WR)", lambda r: simulate_eval_simple(r, e_simple, rules)),
        ("Path-aware, 5 minis, dynamic target", lambda r: simulate_eval_path(r, cache_5m, EVAL_BRACKETS_5M, PT_VALUE_5M)),
        ("Path-aware, 2 minis, dynamic target", lambda r: simulate_eval_path(r, cache_2m, EVAL_BRACKETS_2M, PT_VALUE_2M)),
    ]
    for name, fn in models:
        passes = sum(1 for _ in range(NF) if fn(rng))
        print(f"  {name:<45} {passes/NF:.1%}")

    hist_pass = sum(
        1 for st in range(min(5000, len(entries)))
        if simulate_eval_historical(entries, st, cache_2m, EVAL_BRACKETS_2M, PT_VALUE_2M)
    )
    print(f"  Historical path (2m, sequential days, n=5000 starts)  {hist_pass/5000:.1%}")

    print("\n" + "=" * 78)
    print("TWO-DAY NUKE (backtest)")
    print("=" * 78)
    m4, m32 = MEASURED["4k_recovery"], MEASURED["3200_recovery"]
    print(f"  $4,000:  day1 {m4['p1']:.1%}  day2|loss {m4['p2_cond']:.1%}  2-day seq {m4['p_seq']:.1%}")
    print(f"  $3,200:  day1 {m32['p1']:.1%}  day2|loss {m32['p2_cond']:.1%}  2-day seq {m32['p_seq']:.1%}")

    eval_path_2m = lambda r: simulate_eval_path(r, cache_2m, EVAL_BRACKETS_2M, PT_VALUE_2M)
    eval_simple = lambda r: simulate_eval_simple(r, e_simple, rules)

    print("\n" + "=" * 78)
    print("CAMPAIGN: 10 batches x 10 accts | $5k bankroll")
    print("=" * 78)

    for eval_label, eval_fn in [
        ("path-aware eval (2 minis)", eval_path_2m),
        ("old MC eval (fixed +$1500)", eval_simple),
    ]:
        print(f"\n>>> Eval model: {eval_label}")
        for label, sc, pd1, pd2 in [
            ("$4,000 nuke + recovery", sc4, p1_4, p2_4),
            ("$3,200 nuke + recovery", sc32, p1_32, p2_32),
        ]:
            fs = funded_stats(rng, sc, pd1, pd2, e_flip, eval_fn, rules)
            camp = run_campaign(rng, sc, pd1, pd2, e_flip, eval_fn, rules)
            print(f"\n  --- {label} ---")
            print(f"  Eval pass rate:          {fs['pass']:.1%}")
            print(f"  P(reach payout 1):       {fs['p_payout1']:.1%}  "
                  f"(of funded: {fs['p_payout1_cond']:.1%})")
            print(f"  P(nuke lands):           {fs['p_nuke']:.1%}  "
                  f"(of funded: {fs['p_nuke_cond']:.1%})")
            print(f"  P(ruin mid-campaign):    {camp['ruin']:.1%}")
            print(f"  Mean net profit:         ${camp['mean_profit']:+,.0f}")
            print(f"  Median ending bankroll:  ${camp['median_final']:+,.0f}")
            print(f"  P(end > start):          {camp['p_ahead']:.1%}")
            print(f"  Per-batch mean net:      ${camp['batch_mean']:+,.0f}  "
                  f"P(batch profit) {camp['batch_p_prof']:.1%}")

    print("\n" + "=" * 78)
    print("COPY-TRADING / BATCH SIZE NOTE")
    print("=" * 78)
    print("  Monte Carlo treats each eval account as INDEPENDENT.")
    print("  batch_size=10 means 'buy 10 tickets per round' — not simultaneous trading.")
    print("  Trading 2 evals/day vs 10/day does NOT change per-account pass probability.")
    print("  It only affects calendar time, operational load, and bankroll timing.")
    print("=" * 78)


if __name__ == "__main__":
    main()
