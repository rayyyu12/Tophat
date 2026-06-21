# TopHat NQ — Probability, EV & Risk-of-Ruin

> Monte Carlo over the **locked 50K-DLL lifecycle** (N = 300,000 per scenario, seed 7).
> Reproducible: `python research/probability_report.py`. Win probabilities come from the
> driftless-barrier law for **coinflip** and the 251-day RTH backtest for **drive**
> (see [STRATEGY.md](STRATEGY.md)). This model **validates** against the backtest: it
> reproduces both the ~52% (at a $950 loss-day) and ~39% (at $1,000) eval pass rates and
> the ~45% by-day-2 nuke landing.

## Model & assumptions

| Leg | Size | Bracket | Net win | Net loss | Notes |
|---|---|---|---|---|---|
| **Eval** | 5 minis | 15.5 / 9.5 pt | **+$1,500** | **−$1,000** | target $1,550 gross − ~$50 commission/slippage = ~$1,500 net; pass = +$3,000 over ≥2 days |
| **Nuke** | 2 minis | 80 / 25 pt | +$3,150 | −$1,000 | $3,200 gross target |
| **Re-nuke (day-2 recovery)** | 2 minis | 105 / 25 pt | +$4,150 | −$1,000 | recovers the day-1 loss + nets the nuke |
| **Flip** | 1 mini | 8.5 / 50 pt | +$150 | −$1,000 | $170 gross; risks the full $1,000 DLL |

- **Eval MLL:** $2,000 trailing → floor starts $48,000, trails up, locks at the $50,000 base.
- **Funded MLL:** $2,000 trailing from a $0 start → floor −$2,000, **locks to breakeven ($0)** once the nuke is banked.
- **Lifecycle:** Payout 1 = nuke + 4 flips · Payout 2 = 5 flips · Payout 3 = re-nuke + 4 flips · Payout 4 = 5 flips → **retire at 4 payouts** (each payout = 5 winning days; withdraw `min(50% of profit, $2,000)`).
- Win/loss is decided by the **price** bracket (barrier); dollar amounts are **net** of commission/slippage.

---

## 1. Per-trade odds & expected value (net $)

| Bracket | Coinflip P(win) | Drive P(win) | EV/trade (coinflip) | EV/trade (drive) |
|---|---|---|---|---|
| Eval | 38.0% | **45.6%** | −$50 | **+$140** |
| Nuke | 23.8% | **27.9%** | −$12 | **+$158** |
| Re-nuke (recovery) | 19.2% | 22.5%¹ | −$10 | +$159 |
| Flip | 85.5% | **88.4%** | −$17 | **+$17** |

¹ Re-nuke drive is extrapolated from the nuke edge (not separately backtested).
The **flip is ~breakeven by design** — it banks winning days cheaply, it is not a profit center. The **nuke is the profit engine**.

---

## 2. Eval — odds of passing (50K, $3k target, $2k MLL, $1k DLL)

| Scenario | Pass | Blow | Pass in exactly 2 days | Avg days when it passes |
|---|---|---|---|---|
| Coinflip, fixed target | 26.2% | 73.8% | 14.4% | 3.5 |
| **Drive, fixed target** | **38.8%** | 61.2% | **20.8%** | 3.5 |
| Coinflip, adaptive target | 27.1% | 72.9% | 14.3% | 4.0 |
| Drive, adaptive target | 40.2% | 59.8% | 20.7% | 4.0 |

**Loss-day size is the single biggest eval lever** (the trailing-floor geometry, per STRATEGY §6):

| Realized loss-day (drive, fixed) | Eval pass |
|---|---|
| $950 (raw stop, no slippage) | **51.7%** |
| $1,000 (stop + ~$50 slippage) | **38.7%** |
| $1,050 | 38.2% |

> **Takeaway:** ~1 in 5 evals (drive) pass in the ideal 2 days; the average passer takes ~3.5 days.
> Keeping the loss day at the raw $950 stop (tight slippage control) lifts pass odds from ~39% to ~52%
> — worth more than any other eval tweak.

### Adaptive daily target (your "only need $1,000/$500" point)
Once you are within one win of the $3,000 target, you can shrink that day's target so the day's
win probability rises (a $1,000 day = 10 pt target ≈ 49% coinflip vs 38% at 15.5 pt). Modeled
benefit is **modest: +1.4 pp** (drive 38.8% → 40.2%), because most passes already come from two
full-bracket wins. It is a real, free refinement but a minor one. **Not yet in the engine** — see
"Open items" below.

---

## 3. Nuke — odds of landing the profit bet

| | 1st-try | By day 2 (base → recovery) |
|---|---|---|
| Coinflip | 23.8% | 38.5% |
| **Drive** | **27.9%** | **44.1%** |

A nuke gets **two attempts** (day-1 base, then the wider day-2 recovery) before the $2,000 MLL is
spent. Drive lands it ~44% of the time across the two days.

---

## 4. Funded — full-lifecycle odds & risk of ruin

Per **funded** account, simulated to retirement (4 payouts) or ruin:

| Metric | Coinflip | **Drive** |
|---|---|---|
| Reach payout 1 | 38.0% | **43.8%** |
| Reach payout 2 | 32.0% | 39.6% |
| Reach payout 3 | 9.8% | 14.5% |
| Reach payout 4 (retire) | 9.0% | **13.9%** |
| **Ruin before any payout** | 62.0% | **56.2%** |
| Expected payouts / account | 0.89 | **1.12** |
| Expected $ withdrawn / account | $1,167 | **$1,546** |

> **Reading this:** most funded accounts **die on a nuke** — reaching payout 1 (~44% drive) is
> essentially "did the nuke land." The big drop from payout 2 (40%) to payout 3 (15%) is the
> **re-nuke** at payout 3 killing most survivors. Only ~14% retire with all 4 payouts; the fleet
> **averages ~1.1 payouts**. That is by design: each funded account is a cheap, high-variance bet,
> not a sure annuity.

---

## 5. End-to-end EV per eval ticket (drive)

| | Conservative ($1,000 loss-day) | Optimistic ($950 loss-day, = backtest) |
|---|---|---|
| P(eval pass) | 38.7% | ~52% |
| E[$ withdrawn] / funded account | $1,546 | $1,546 |
| **EV per $85 eval ticket** | **+$514** | **~+$791** |
| **ROI** | **+605%** | **~+930%** |

The optimistic column matches [STRATEGY.md](STRATEGY.md) §4 (+$791 / +930%). The spread is driven
almost entirely by the eval loss-day assumption. **Either way the ticket is strongly +EV** — the
structural asymmetry (cheap eval → multi-payout funded account) is the engine, exactly as the
coinflip controls predict.

---

## 6. Risk of ruin — account vs. bankroll

- **Single-account "ruin" is high and expected** (~56% blow before payout 1). That is not a bankroll
  risk — it is the cost of a +EV lottery ticket.
- **Bankroll ruin** (going broke across reinvested tickets) is the number that matters, and it
  depends on sizing. Per STRATEGY.md, a **$5,000+ bankroll keeps modeled ruin < 1%**; real ruin is
  somewhat higher because cross-account correlation is unmodeled — keep a buffer, don't run to the edge.
- **Decorrelation controls** that hold ruin down: ≤1 nuke/day spread across the week, ≤2 evals/day,
  and staggered flip entries (all enforced by the scheduler).

---

## Open items / caveats

- **Adaptive eval target** (§2) is modeled but **not implemented** in `engine.py` (decide() always
  uses the full 15.5 pt eval bracket). Benefit is small (+~1.4 pp); implement if desired.
- Re-nuke **drive** win-rate is extrapolated, not separately backtested.
- Model assumes per-day independence (no cross-account correlation, no calendar/news effects) and a
  flat ~$50 commission/slippage buffer — same caveats as the backtest (STRATEGY §2).
- Numbers regenerate with `python research/probability_report.py` (local-only script).
