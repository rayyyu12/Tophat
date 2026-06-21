# TopHat NQ — Locked Strategy & EV Summary

> Status: **research/backtest phase complete, configuration LOCKED.** Audited against the
> 251-day 1s RTH cache (2025-06-23 → 2026-06-17). This document is the single source of truth
> for *what we trade and why*. The engineering plan lives in [BUILD_PLAN.md](BUILD_PLAN.md).

---

## 1. The decision in one table

| Decision | Locked choice | Why |
|---|---|---|
| **Account** | **Topstep 50K, WITH DLL** (~$85/ticket) | +930% EV/ticket vs ~+558% for the 150K. Cheaper, easier nuke, 2 full nuke tries, easier eval. (See §4.) |
| **Eval size** | **5 minis**, 15.5pt / 9.5pt bracket, $3,000 target | The only statistically-significant edge (+7.6pp). 1 trade/day, drive direction. |
| **Funded — nuke** | **2 minis, $3,200 target** (80pt / 25pt stop), day-2 recovery to ~$4,200 (105pt / 25pt) | Near-optimal given the $2,000 payout cap. ~45% to land the first payout over 2 days. |
| **Funded — flip** | **1 mini, $170 target** (8.5pt), stop = full $1,000 DLL (50pt) | Same win probability as 2 minis, **half the commission**. A winning-day generator, not a profit center. |
| **Lifecycle** | nuke + 4 flips → 5 flips → re-nuke + 4 flips → 5 flips, **retire at 4 payouts** | Harvest below the ~5-payout live-account trigger. Re-nuke only gambled after 2 payouts are banked. |
| **Eval batching** | 10 evals/batch, copy at most **2 per day** | Cheap tickets; copy ≤2 limits correlated eval exposure. |
| **Nuke scheduling** | **One account/day, spread across the week** (keep the 09:45 ET entry) | Decorrelates the high-variance bet across days — the single most important risk control. |
| **Flip scheduling** | **All accounts daily, staggered entry times** (09:45 / 10:00 / 10:15 / 10:30 / 10:45 ET) | Fast (whole fleet finishes 5 winning days in ~1 week) *and* decorrelated. Free, because flips carry ~no edge to lose. |
| **Bankroll** | $5K+ keeps modeled ruin <1% | Real ruin is higher than modeled (residual correlation) — keep a buffer, don't run it to the edge. |

---

## 2. Where the edge actually comes from (read this before trusting the numbers)

There are **two** edges, and they are not equally strong:

1. **Structural EV (the real engine, verified).** A ~$85 eval ticket converts into a funded
   account capable of multiple ~$2,000 payouts. The Monte Carlo is **net-positive even at a pure
   coin flip** (zero predictive edge): DLL/coin batch ROI ≈ **+282%**. The coin-flip controls
   reproduce the theoretical driftless barrier probabilities to within **0.4pp** on every bracket,
   which empirically *proves* NQ intraday behaves like a driftless random walk at the bar level —
   so `P(win) = stop / (target + stop)` is the real probability, not an assumption. This is the
   bedrock.

2. **Drive-momentum edge (real but thin).** Enter at 09:45 ET in the direction of the 09:30–09:45
   opening range. It roughly **doubles** the structural EV and **halves** ruin, but it is small and
   only the eval leg is individually significant:

   | Bracket | Baseline | Drive | 95% CI | Edge | Significant? |
   |---|---|---|---|---|---|
   | Eval 15.5/9.5 | 38.0% | 45.6% | [39.5%, 51.8%] | +7.6pp | **Yes** |
   | Flip 8.5/50 | 85.5% | 88.4% | [83.8%, 91.8%] | +2.9pp | No (CI spans baseline) |
   | Nuke 80/25 ($3,200) | 23.8% | 27.9% | [22.7%, 33.8%] | +4.1pp | No |

   Drive is the *best* of three pre-registered signals (sweep and breakout were rejected) and is
   positive on **all** brackets, with the edge largest on the most directional bets — a coherent
   "momentum helps a bigger directional bet" story. It held out-of-sample where breakout overfit
   and collapsed. **Treat the drive edge as upside, not the thesis. Size as if you are near coin
   flip.**

> ⚠️ **Caveats that make the model optimistic** (none flip the sign of EV, but they widen risk):
> cross-account correlation is unmodeled (independence assumed); commissions and *exit* slippage are
> not modeled (flip edge falls to +0.9pp at 2 ticks of entry slippage); no calendar time; one year of
> data with a known weak regime (Nov–Feb); Topstep payout rules unconfirmed.

---

## 3. The flip, correctly framed

A flip risks the full $1,000 DLL to make $170 — an ~88% win rate. At baseline (no edge) a flip is
**exactly breakeven** by construction; it only makes money via the (thin, slippage-fragile) drive
edge. **Do not model flips as profit.** Their job is to bank "winning days" cheaply to unlock
payouts. A near-breakeven 88%-win trade does that perfectly. The nuke is the profit engine.

**1 mini vs 2 minis is settled: identical win rate** (the backtest resolves in points; contract count
is only a dollar multiplier). 1 mini wins purely on commission. Same logic killed the 150K "flip
advantage" — see §4.

---

## 4. Why 50K beats 150K (the comparison that was run)

The 150K's wider $3,000 DLL makes a flip win 96% of the time — but that is an **EV illusion**:
expected loss per flip is **$120 on both** accounts (150K: 4% × $3,000; 50K: 12% × $1,000). Higher
win rate, exactly offset by a bigger tail loss. End-to-end, per purchased ticket:

| Account / nuke | Eval pass | Nuke 1st-try | $/funded acct | EV/ticket | **ROI** |
|---|---|---|---|---|---|
| **50K — $3,200 ($85)** | ~52% | 30% | $1,699 | +$791 | **+930%** |
| 150K — $6,000 ($195) | ~41% | 39% | $3,144 | +$1,087 | +558% |
| 150K — $10,000 ($195) | ~41% | 22% | $2,536 | +$839 | +430% |

The 150K makes more *absolute* EV per account (bigger payout cap) but far lower ROI (ticket costs
2.3×). To fill its $5,000 cap you need a ~$10,000 nuke = a 167-point intraday target, where **11% of
days never resolve and the drive edge goes negative**. Its second chance is also weaker (1.5 tries
vs 2). **50K wins for a bankroll-building, reinvesting operation.** Revisit the 150K only if account
count / attention (not capital) becomes the binding constraint — and if so, nuke ~$6,000, never $10,000.

---

## 5. EV parking lot (ideas — untested, do NOT block launch)

Ordered roughly by promise. All require pre-registered IS/OOS testing to avoid the overfitting trap.

- **Two validated edge windows.** The nuke edge appears at **both** 09:30–09:45 (→09:45 entry, 27.9%)
  *and* 10:00–10:15 (→10:15 entry, 27.6%), but **not** 09:45–10:00 (19.8%, below coin). This means
  nukes/evals could be split across the two good windows for *edge-preserving decorrelation* — unlike
  flips (which decorrelate at any window because they carry no edge).
- **Regime / volatility filter on the nuke.** Skip the nuke on dead, narrow-range mornings (e.g. only
  fire if the 09:30–09:45 range exceeds a fraction of recent ATR). Directly targets the known
  choppy-regime weakness. Highest-promise improvement.
- **News-day tilt.** CPI / FOMC / NFP mornings produce the big directional moves nukes need.
  Concentrating nukes there may raise the big-move rate.
- **Skip the re-nuke in choppy regimes.** Payout-3 re-nuke is a pure gamble; could be conditioned on
  regime once 2 payouts are banked.
- **Nuke target fine-tune.** $3,200 is near-optimal vs the $2,000 cap; a sweep of $2,800–$3,400 could
  find the exact peak. Minor.
- **The biggest lever is not a trading edge** — it is surviving Topstep enforcement (retire at 4
  payouts, independent accounts, spread activity). Spend effort there, not on edge-hunting.

---

## 6. Must-confirm before live money (non-statistical gates)

1. **Topstep payout rules:** first-payout cap, withdrawal %, minimum days to/between payouts, the
   live-account trigger (we assume ~5 → retire at 4). These determine whether $3,200 is optimal.
1b. **Account balances:** ✅ **CONFIRMED.** Eval combines start at **$50,000**; Express **funded
   accounts start at $0** with a **−$2,000** trailing max-loss floor that locks at breakeven once
   earned. An eval and the funded account that replaces it are **separate accounts** (the eval is
   deleted ~15–30 min after passing and a new funded account is created). The engine models this:
   `funded_initial_balance=0`, per-account `base_balance`, and balance-based phase inference. Phases
   `passed` (green) and `blown` (red) are surfaced on the dashboard.

2. **Trailing-drawdown mechanic:** ✅ **CONFIRMED end-of-day.** Unrealized intraday P&L does *not*
   move the DLL or the max-loss floor; they update only on the daily close, then the new day starts.
   This is favorable — no risk of an intraday spike-then-give-back tripping the trailing floor, and the
   engine already models it via `peak_equity_eod`. (The eval pass rate is still somewhat sensitive to
   the exact loss-day size vs trailing room — a $950 vs $1,000 losing day shifts 50K eval pass ~52% vs
   ~39% — but that's a geometry detail, not the intraday/EOD ambiguity, which is now settled.)
3. **Reconcile the engine to this spec** — see [BUILD_PLAN.md](BUILD_PLAN.md) §2. `tophat/engine.py`
   still defaults to a $4,000 nuke and a 2-mini $150 flip; the backtested numbers only transfer once
   the live brackets match.
