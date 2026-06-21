"""
Monte Carlo: eval batching strategies with copy-trade correlation.

Copy-trading N accounts on the same signal = ONE daily outcome, perfectly correlated.
That is NOT N independent tries. This sim quantifies the difference.

Strategies (per batch of 10 tickets):
  copy10      — copy all 10; 1 eval path for the whole batch
  copy2_x5    — 5 pairs, each pair shares 1 path (2 correlated, pairs independent)
  independent — 10 independent eval paths (upper bound; different days / uncorrelated)
  serial1_x10 — 10 sequential single-account paths (same as independent in MC)

Campaign: 3 batches x 10 accounts, $5k bankroll, path-aware 5-minis eval.

Usage:
    python research/monte_carlo_eval_batching.py
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from research.monte_carlo import DRIVE_PP_DLL, Rules, _clip, _floor
from research.monte_carlo_scenarios import (
    MEASURED,
    NukeScenario,
    probs_for,
    simulate_funded,
)

# Reuse path-aware eval from run_campaign_compare
from research.run_campaign_compare import (
    EVAL_BRACKETS_5M,
    PASS_PROFIT,
    CONSISTENCY_CAP,
    DLL,
    INITIAL,
    TRAILING,
    EVAL_MAX_DAYS,
    PT_VALUE_5M,
    _fill_wr_cache,
    bracket_for_remaining,
    wr_lookup,
)
from research.backtest_common import collect_drive_entries, load_days

EVAL_COST = 85.0
BATCH_SIZE = 10
N_BATCHES = 3
BANKROLL = 5_000.0
TRIALS = 10_000
SEED = 42


class Strategy(str, Enum):
    COPY10 = "copy10_all"
    COPY2 = "copy2_x5pairs"
    INDEPENDENT = "independent10"


def simulate_eval_path(rng, cache: dict, brackets) -> bool:
    eq = INITIAL
    peak = eq
    days = 0
    while days < EVAL_MAX_DAYS:
        remaining = PASS_PROFIT - (eq - INITIAL)
        if remaining <= 0 and days >= 2:
            return True
        tgt, stp, win_d = bracket_for_remaining(remaining, PT_VALUE_5M, brackets)
        p = wr_lookup(tgt, stp, cache, brackets)
        days += 1
        if rng.random() < p:
            eq += min(win_d, CONSISTENCY_CAP)
        else:
            eq -= DLL
        peak = max(peak, eq)
        if eq <= _floor(INITIAL, peak, TRAILING):
            return False
    return False


def simulate_batch(rng, strategy: Strategy, cache, brackets) -> int:
    """Return number of eval passers in a batch of 10."""
    if strategy == Strategy.COPY10:
        passed = simulate_eval_path(rng, cache, brackets)
        return BATCH_SIZE if passed else 0

    if strategy == Strategy.COPY2:
        passers = 0
        for _ in range(5):
            if simulate_eval_path(rng, cache, brackets):
                passers += 2
        return passers

    # independent10 / serial
    return sum(1 for _ in range(BATCH_SIZE) if simulate_eval_path(rng, cache, brackets))


def simulate_campaign(rng, strategy: Strategy, cache, brackets,
                      sc, p1, p2, e_flip, rules) -> dict:
    bankroll = BANKROLL
    batch_cost = EVAL_COST * BATCH_SIZE
    total_pass = 0
    total_withdrawn = 0.0
    batches_zero = 0
    ruined = False

    for b in range(N_BATCHES):
        if bankroll < batch_cost:
            ruined = True
            break
        bankroll -= batch_cost
        n_pass = simulate_batch(rng, strategy, cache, brackets)
        total_pass += n_pass
        if n_pass == 0:
            batches_zero += 1
        for _ in range(n_pass):
            total_withdrawn += simulate_funded(rng, rules, e_flip, sc, p1, p2).withdrawn

    return {
        "passers": total_pass,
        "withdrawn": total_withdrawn,
        "final": bankroll + total_withdrawn,
        "net": bankroll + total_withdrawn - BANKROLL,
        "batches_zero": batches_zero,
        "all_batches_zero": batches_zero == N_BATCHES,
        "ruined": ruined,
    }


def main() -> None:
    rng = np.random.default_rng(SEED)
    days = load_days()
    entries = collect_drive_entries(days)
    cache: dict = {}
    _fill_wr_cache(entries, EVAL_BRACKETS_5M, PT_VALUE_5M, cache)

    rules = Rules(payouts_target=4)
    pp = DRIVE_PP_DLL
    e_flip = _clip(rules.dll / (rules.flip_target_dollars + rules.dll) + pp["flip"])
    sc32 = NukeScenario("3200", 3_200, 1_000, True, "recovery", "3200_recovery")
    p1, p2, _ = probs_for(sc32, rules, True)

    p_pass = sum(1 for _ in range(20_000) if simulate_eval_path(rng, cache, EVAL_BRACKETS_5M)) / 20_000

    print("=" * 78)
    print("EVAL BATCHING MONTE CARLO  |  3 batches x 10 accts  |  $5k bankroll")
    print("=" * 78)
    print("\n--- Backtest basis (actual historical NQ 1s data, drive signal) ---")
    print("  Win rates are NOT guessed. Each bracket is resolved bar-by-bar on ~251")
    print("  RTH days using the same 1s cache as backtest.py (next-bar fill, drive @ 09:45).")
    print("  5 minis: 15.5/9.5 pt ($1550/~$950) -> 45.6% drive WR per full day.")
    print("  2 minis: 37.5/25 pt ($1500/$1000 DLL) -> 42.3% (different pt geometry).")
    print(f"  Path-aware 5-minis single-account eval pass rate: {p_pass:.1%}")
    print("\n--- Copy-trade correlation ---")
    print("  COPY ALL 10: 1 trade outcome -> all 10 accounts move together.")
    print("  P(0 passers in batch) = P(single fail) ~ {:.1%}  NOT P(fail)^10.".format(1 - p_pass))
    print("  INDEPENDENT 10: 10 separate paths -> P(0 passers) ~ P(fail)^10.")
    print()

    hdr = f"{'Strategy':<18} {'E[pass]':>7} {'P(0/batch)':>10} {'P(0x3 bat)':>10} {'P(all30 fail)':>13} {'E[net$]':>9} {'P(prof)':>7}"
    print(hdr)
    print("-" * len(hdr))

    for strat in Strategy:
        results = [
            simulate_campaign(rng, strat, cache, EVAL_BRACKETS_5M, sc32, p1, p2, e_flip, rules)
            for _ in range(TRIALS)
        ]
        passers = np.array([r["passers"] for r in results])
        batch_zero = np.array([r["batches_zero"] for r in results])
        all30_fail = passers == 0
        all3_batch_zero = np.array([r["all_batches_zero"] for r in results])
        nets = np.array([r["net"] for r in results])

        # P(all 30 fail eval) = no passers across campaign
        # Per-batch P(0 passers)
        p0_batch = np.mean(passers == 0)

        print(f"{strat.value:<18} {passers.mean():>7.2f} {p0_batch:>9.1%} "
              f"{np.mean(all3_batch_zero):>9.1%} {np.mean(all30_fail):>12.1%} "
              f"${nets.mean():>+8,.0f} {np.mean(nets > 0):>6.1%}")

    print("\n--- Per-batch detail (one batch of 10) ---")
    for strat in Strategy:
        dist = {k: 0 for k in range(11)}
        for _ in range(TRIALS):
            n = simulate_batch(rng, strat, cache, EVAL_BRACKETS_5M)
            dist[n] = dist.get(n, 0) + 1
        print(f"\n  {strat.value}:")
        for k in range(11):
            if dist[k]:
                print(f"    {k:>2} passers: {dist[k]/TRIALS:>5.1%}")

    print("\n" + "=" * 78)
    print("RECOMMENDATION SUMMARY")
    print("=" * 78)
    print("""
  EXPECTED pass count is ~the same (~4 per 30 tickets) regardless of batching.
  What changes is VARIANCE — how lumpy results are.

  COPY ALL 10: Highest variance. One bad eval week = 0/10. Feels like '1 try'
               per batch, not 10. Over 3 batches you have ~3 independent 'super-
               tries' (if batches are on different weeks), not 30.

  COPY 2 (5 pairs): Middle ground. One pair failing doesn't kill the other 4
               pairs. P(0/10 in batch) much lower than copy-10.

  INDEPENDENT: Theoretical best diversification (different start days / no copy).
               P(all 30 fail) is tiny. Hard to achieve perfectly if you still
               copy within a pair.

  PRACTICAL: Buy batches of 10 (cost efficiency), but COPY TRADE max 2 at a time
             on different start dates within the batch. Stagger pairs across the
             week so pairs don't all see the same losing week.

  Use 5 MINIS on eval (45.6% full-day WR on real data). Funded stays 2 minis.
""")
    print("=" * 78)


if __name__ == "__main__":
    main()
