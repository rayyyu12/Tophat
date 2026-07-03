# TopHat NQ — Probability, EV & Risk-of-Ruin

> Monte Carlo over the **locked 50K-DLL lifecycle**, corrected for the real
> **intraday** Maximum Loss Limit. Reproducible: `python research/probability_intraday.py`
> (N = 200,000, seed 7). Win probabilities are the **real (target, stop) barriers
> resolved bar-by-bar** on the 251-day 1s RTH drive cache (same data as backtest.py).

## 0. CORRECTION (2026-06-24) — intraday MLL + $1,000 stop

Two things changed versus the earlier version of this doc, after confirming the
mechanics against help.topstep.com:

1. **The Combine MLL breaches in real time on UNREALIZED P&L** — *"monitored in
   real time… both realized and unrealized P&L count… liquidated immediately."*
   The floor only **ratchets up at end of day** and **locks at the $50,000 start**
   once your EOD balance reaches $52,000. So near the floor your *effective* stop
   is the remaining room, not your 9.5pt strategy stop. The earlier models
   (`probability_report.py`, `monte_carlo.py`, `run_campaign_compare.py`) checked
   the floor only at EOD after booking a full-stop loss, which inflated near-floor
   recovery — this is what produced the bogus "~52% optimistic" eval pass rate.

2. **Stop is now $1,000 (10pt at 5 minis) = the full DLL**, not the 9.5pt $950
   stop. When remaining room ≤ $1,000 we set **no manual stop** and let Topstep
   auto-liquidate at the floor (a non-win there = a clean, certain blow). A
   $1,000 stop is exactly half the $2,000 MLL, so the account is always a whole
   number of "$1,000 lives" from the floor (2 → 1 → dead) and **never sits in a
   fractional-room state** — so intraday-MLL and EOD-MLL now give the *same* eval
   pass rate (42.4%). The correction's real effect: discard the optimistic ~52%
   figure, and a modest cut to funded EV (the re-nuke and post-withdrawal flips
   start with less room).

**Net:** eval pass **~42%** (was quoted ~39% conservative / ~52% optimistic),
EV/ticket **+$530 / +623% ROI** (was +$514 / +$791).

---

## Model & assumptions

| Leg | Size | Bracket | Net win | Net loss | Notes |
|---|---|---|---|---|---|
| **Eval** | 5 minis | 15.0 / **10.0** pt | **+$1,500** | **−$1,000** | $1,500 = 50% consistency cap; **stop = full $1,000 DLL**; pass = +$3,000 over ≥2 days |
| **Nuke** | 2 minis | 80 / 25 pt | +$3,150 | −$1,000 | $3,200 gross target |
| **Re-nuke (day-2)** | 2 minis | 105 / 25 pt | +$4,150 | −$1,000 | recovers the day-1 loss + nets the nuke |
| **Flip** | 1 mini | 8.5 / 50 pt | +$150 | −$1,000 | $170 gross; risks the full $1,000 DLL |

- **MLL (both phases):** $2,000 trailing, **breached intraday on unrealized P&L**,
  ratchets up at EOD only, **locks at the starting balance** (eval → $50,000 floor;
  funded $0 start → $0 / breakeven floor).
- **Effective stop each day = min($1,000, room-to-floor).** Room ≤ $1,000 ⇒ a
  non-win is a certain blow (auto-liquidation).
- **Consistency rule (real Topstep):** best day ÷ total profit ≤ 50%, target
  auto-raises to `best/0.50` if exceeded ⇒ **pass iff days ≥ 2 and total ≥
  max($3,000, 2 × best_day)**. Modeled with a final-day target shrink.

---

## 1. Per-trade odds & expected value (net $, drive, data-resolved)

| Bracket | Drive P(win) | EV/trade |
|---|---|---|
| Eval ($1,500 day, 15/10pt) | **46.6%** | +$232 |
| Nuke (80/25pt) | **27.9%** | +$159 |
| Re-nuke (105/25pt) | 23.6% | +$209 |
| Flip (8.5/50pt) | **88.4%** | +$18 |

The **flip is ~breakeven by design** — it banks winning days cheaply. The **nuke
is the profit engine.**

---

## 2. Eval — odds of passing (50K, $3k target, $2k MLL, $1k stop)

Conservative $1,500/day policy with final-day shrink (`research/probability_intraday.py`):

| Model | Pass | Blow | E[days · pass] | Pass in 2 days | Pass in ≤4 days |
|---|---|---|---|---|---|
| **Corrected (intraday MLL)** | **42.4%** | 57.6% | 3.44 | 21.6% | 34.6% |
| old EOD model, $1,000 stop | 42.5% | 57.5% | 3.45 | 21.6% | 34.6% |

> The two agree because a $1,000 stop leaves no fractional-room state to mishandle.
> **The discarded artifact was the $950-stop / EOD model, which read ~52%.**

**Target sizing is settled: keep ~$1,500/day.** Going bigger is dominated — the
consistency rule auto-raises the bar with your best day, so a bigger target can't
shorten the path after a loss (it's mathematically impossible to pass in 2 winning
days after any loss) and only lowers your daily win rate. Aggressive $2,000/day
drops the pass rate ~10pp with no speed gain (`research/eval_scheduling_and_target.py`).

---

## 3. Nuke — odds of landing the profit bet

| | 1st-try | By day 2 (base → recovery) |
|---|---|---|
| **Drive** | **27.9%** | **~44.5%** |

A nuke gets two attempts (day-1 base, then the wider day-2 recovery) before the
$2,000 MLL is spent. The nuke's room never drops below its $1,000 stop, so the
intraday correction does not change it.

---

## 4. Funded — full-lifecycle odds & risk of ruin (intraday MLL)

Per funded account, simulated to retirement (4 payouts) or ruin:

| Metric | **Drive (corrected)** |
|---|---|
| Reach payout 1 | **44.5%** |
| Reach payout 2 | 38.1% |
| Reach payout 3 | 11.0% |
| Reach payout 4 (retire) | **10.4%** |
| Ruin before any payout | **55.5%** |
| Expected payouts / account | **1.04** |
| Expected $ withdrawn / account | **$1,450** |

> Most funded accounts die on a nuke (reach payout 1 ≈ "did the nuke land"). The
> big drop from payout 2 → 3 is the **re-nuke** killing most survivors — and the
> intraday correction makes it slightly harsher, because the re-nuke and the
> post-withdrawal flip cycles start with less room than the old model assumed.

---

## 5. End-to-end EV per eval ticket (drive, corrected)

| | Value |
|---|---|
| P(eval pass) | **42.4%** |
| E[$ withdrawn] / funded account | $1,450 |
| **EV per $85 eval ticket** | **+$530** |
| **ROI** | **+623%** |

The structural asymmetry (cheap eval → multi-payout funded account) is still the
engine and still strongly +EV. The earlier "+930%" headline used the inflated
~52% eval pass and should be retired; **+623% is the honest number.**

---

## 6. Throughput — 10 evals, depth-first pipeline

Per-account pass probability is identical under any schedule (~4.2 funded/10).
Scheduling changes only calendar latency. Depth-first (advance the most-progressed
eval to completion, ≤2 trades/day) vs the time to fully clear a batch:

| Slots/day | Funded / 10 | 1st pass | 3rd pass | **All 10 resolved** |
|---|---|---|---|---|
| 2 | 4.24 | day 4.5 | day 10.7 | **day 17.1 (~3.4 wk)** |
| 3 | 4.25 | day 3.5 | day 7.7 | **day 12.2 (~2.4 wk)** |

(Trading days; "resolved" = passed or blown.) Slots/day is the throughput lever —
same expected passes, more correlation. Cap at 2/day staggered across the two
validated edge windows (09:45 / 10:15 ET) to keep the daily pair decorrelated.

---

## Open items / caveats

- **Live execution now matches the model (2026-06-24):** the eval stop is $1,000
  (10pt) in config/engine, and both fire paths (`server/service.py`,
  `services/runner.py`) drop the manual stop when room ≤ the day's stop via
  `engine.should_omit_stop`, letting Topstep auto-liquidate at the floor
  (`brackets.plan_to_order` omits `stopLossBracket`). Tests in
  `tests/test_near_floor_stop.py`.
- **Legacy MC scripts** (`probability_report.py`, `monte_carlo.py`,
  `run_campaign_compare.py`) retain the EOD-only floor check; they are superseded
  by `research/probability_intraday.py` for the eval/EV numbers.
- Re-nuke drive win-rate is data-resolved (105/25pt) rather than separately
  twoday-backtested.
- Model assumes per-day independence (no cross-account correlation, no calendar/
  news effects) and a flat ~$50 commission/slippage buffer. Entry slippage on
  near-floor trades would push those (already rare) recoveries lower still.
- **Feb-2026 Topstep "Consistency Path"** (3-day XFA progression at 40%) is not
  yet modeled — a funded-stage payout option that may change the optimal lifecycle.
