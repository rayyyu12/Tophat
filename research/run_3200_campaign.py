"""One-off: P(first payout) and 10x10 campaign for $3200 recovery nuke."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from research.monte_carlo import DRIVE_PP_DLL, Edge, Rules, _clip, simulate_eval
from research.monte_carlo_scenarios import (
    MEASURED,
    NukeScenario,
    probs_for,
    simulate_funded,
    simulate_nuke,
)

NF = 50_000
BS = 10
ROUNDS = 10
BANK = 5_000
CAMPAIGN_TRIALS = 5_000
SEED = 42


def p1_cycle(rng, rules, sc, p1, p2, e_flip) -> bool:
    """Nuke (2-day) + 4 flips -> payout 1 eligible."""
    hit, delta, _ = simulate_nuke(rng, sc, rules, p1, p2)
    if not hit:
        return False
    eq = rules.initial_balance + delta
    peak = eq
    wins = 1
    for _ in range(30):
        if wins >= rules.winning_days_required:
            break
        if rng.random() < e_flip:
            eq += rules.flip_target_dollars
            wins += 1
        else:
            eq -= rules.funded_day_stop
        peak = max(peak, eq)
        floor = min(rules.initial_balance, peak - rules.trailing_drawdown)
        if eq <= floor:
            return False
    return wins >= rules.winning_days_required and eq > rules.initial_balance


def main() -> None:
    rng = np.random.default_rng(SEED)
    rules = Rules(nuke_target=3_200, has_dll=True, payouts_target=4)
    pp = DRIVE_PP_DLL
    e_eval = _clip(
        rules.eval_stop_pts / (rules.eval_target_pts + rules.eval_stop_pts) + pp["eval"]
    )
    e_flip = _clip(rules.dll / (rules.flip_target_dollars + rules.dll) + pp["flip"])

    sc4 = NukeScenario("4k", 4_000, 1_000, True, "recovery", "4k_recovery")
    sc32 = NukeScenario("3200", 3_200, 1_000, True, "recovery", "3200_recovery")
    p1_4, p2_4, _ = probs_for(sc4, rules, True)
    p1, p2, _ = probs_for(sc32, rules, True)

    m4 = MEASURED["4k_recovery"]
    m32 = MEASURED["3200_recovery"]

    print("=" * 72)
    print("TWO-DAY NUKE SEQUENCE (backtest measured, drive, recovery brackets)")
    print("=" * 72)
    for label, m in [("$4,000 target", m4), ("$3,200 target", m32)]:
        d2 = (1 - m["p1"]) * m["p2_cond"]
        print(f"\n{label}")
        print(f"  Day-1 win rate:              {m['p1']:.1%}")
        print(f"  Day-2 win (if day-1 loss):   {m['p2_cond']:.1%}")
        print(f"  Win on day-1 only:           {m['p1']:.1%}")
        print(f"  Win on day-2 after loss:     {d2:.1%}")
        print(f"  2-day sequence success:      {m['p_seq']:.1%}")

    print("\n" + "=" * 72)
    print("P(FIRST PAYOUT) — P1 = NUKE (2 tries) + 4 FLIPS  |  drive flip {:.1%}".format(e_flip))
    print("=" * 72)

    for label, sc, p1x, p2x in [
        ("$4k recovery", sc4, p1_4, p2_4),
        ("$3200 recovery", sc32, p1, p2),
    ]:
        nuke_land = sum(
            1 for _ in range(NF) if simulate_nuke(rng, sc, rules, p1x, p2x)[0]
        )
        p1_ok = sum(1 for _ in range(NF) if p1_cycle(rng, rules, sc, p1x, p2x, e_flip))
        fr = [simulate_funded(rng, rules, e_flip, sc, p1x, p2x) for _ in range(NF)]
        print(f"\n{label}")
        print(f"  P(nuke lands within 2 days):     {nuke_land / NF:.1%}")
        print(f"  P(complete P1 -> payout 1):      {p1_ok / NF:.1%}")
        print(f"  P(reach payout 1, full sim):    {np.mean([x.payouts >= 1 for x in fr]):.1%}")
        print(f"  P(bust before any payout):       {np.mean([x.busted for x in fr]):.1%}")
        print("  Lifecycle: " + "  ".join(
            f"P{k} {np.mean([x.payouts >= k for x in fr]):.1%}" for k in range(1, 5)
        ))

    print("\n" + "=" * 72)
    print("CAMPAIGN: 10 batches x 10 accounts | $3200 recovery | bankroll $5,000")
    print("Lifecycle: P1 nuke+4flips | P2 5flips | P3 re-nuke+4flips | P4 5flips")
    print("=" * 72)

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
                acct_net = -rules.eval_cost
                if simulate_eval(rng, rules, Edge(eval_win=e_eval)):
                    acct_net += simulate_funded(rng, rules, e_flip, sc32, p1, p2).withdrawn
                batch_w += acct_net
            bankroll += batch_w
            batch_nets.append(batch_w)
        finals.append(bankroll)
        profits.append(bankroll - BANK)
        ruins += int(ruined)

    finals = np.array(finals)
    profits = np.array(profits)
    batch_nets = np.array(batch_nets)

    print(f"\n  Eval cost per batch: ${batch_cost:,}  |  Total spend if all 10 run: ${batch_cost * ROUNDS:,}")
    print(f"  P(ruin mid-campaign):              {ruins / CAMPAIGN_TRIALS:.1%}")
    print(f"  Mean ending bankroll:              ${finals.mean():+,.0f}")
    print(f"  Median ending bankroll:            ${np.median(finals):+,.0f}")
    print(f"  Mean net profit (end - $5k start): ${profits.mean():+,.0f}")
    print(f"  P(end > start):                    {np.mean(finals > BANK):.1%}")
    print(f"\n  Per-batch net (10 accts, after ticket cost):")
    print(f"    Mean:   ${batch_nets.mean():+,.0f}")
    print(f"    Median: ${np.median(batch_nets):+,.0f}")
    print(f"    P(batch profitable): {np.mean(batch_nets > 0):.1%}")
    print("=" * 72)


if __name__ == "__main__":
    main()
