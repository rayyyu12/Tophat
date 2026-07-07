# Statistics audit — 2026-07-06

Independent re-verification of every strategy statistic on the **real
tick-derived cache** (285 days, 264 usable @09:45 entries, 2025-06-23 →
2026-06-30, pessimistic ties). Methodology: one unified lifecycle Monte Carlo
(12k paths/firm, seed 11, ≤250 trading days/ticket) with the REAL per-firm
rules — net-of-commission booking ($7/mini round trip; sensitivity at $0/$20),
win-day minimums on NET P&L, eval consistency rules (50%/50%/40%/none), Apex
payout gate (balance ≥$2,600 + 50% window consistency + $1,500 requests +
plain re-nuke + retire at 6), Topstep-family recovery re-nuke and
min($2,000, balance/2) payouts, trailing $2,000 MLL with breakeven lock.
Audit script: session scratchpad `audit_stats.py`. Predecessors:
docs/MULTI_FIRM_PLAN.md §3 (251-day cache, gross booking),
docs/PROBABILITY.md.

## 1. The edge, re-verified (drive vs coin, first-touch on real bars)

| Leg | Drive WR | Coin WR | Edge | analytics.MODEL_WR (coin/drive) |
|---|---|---|---|---|
| TS eval 15.5/10 ×5 | **50.0%** | 40.4% | +9.6pp | .400/.466 ✓ (drive +3.4pp better on extended data) |
| TS nuke 80/25 ×2 | 28.6% | 23.4% | +5.2pp | .238/.279 ✓ |
| TS renuke 105/25 ×2 | 24.0% | 19.5% | +4.5pp | .192/.236 ✓ |
| TS flip 8.5/50 ×1 | 90.5% | 85.6% | +4.9pp | .855/.884 ✓ |
| AX eval 30/10 ×5 | 30.3% | 23.2% | +7.1pp | .250/.302 ✓ |
| AX nuke 32.5/25 ×2 | 48.7% | 42.0% | +6.7pp | .435/.488 ✓ |
| AX flip 16.25/50 ×1 | 81.7% | 76.2% | +5.5pp | .755/.815 ✓ |

Verdict: the drive edge is intact in the 13 added days; every MODEL_WR
constant agrees with fresh measurement within ~2pp. No stale numbers found in
code (`analytics.MODEL_WR`, `engine.SIGNAL_PLAN_TEMPLATES`, `store/firms.py`,
live settings) — all consistent with docs.

## 2. Where the strategy becomes unprofitable (edge-fade sweep)

Fade parameter q: 1 = full drive edge, 0 = pure coin flip, −1 = the signal is
fully ANTI-predictive (every leg's WR mirrored below its coin floor).
Mean net $/ticket:

| q | −1.00 | −0.75 | −0.50 | −0.25 | 0 (coin) | +0.50 | +1.00 (drive) | break-even |
|---|---|---|---|---|---|---|---|---|
| Topstep | +35 | +101 | +133 | +200 | **+262** | +488 | **+739** | **< −1** |
| Lucid | +22 | +88 | +120 | +187 | +249 | +475 | +726 | < −1 |
| Tradeify | −3 | +46 | +83 | +203 | +262 | +512 | +877 | ≈ −0.98 |
| Apex | −20 | −2 | +22 | +58 | +103 | +271 | +655 | ≈ −0.73 |

In WR terms: Topstep stays +EV even with eval WR at ~31% (vs 40.4% coin
floor), i.e. the signal must be persistently WRONG before a ticket loses
money. Apex is the most edge-sensitive (break-even ≈ eval 18% / nuke 37% /
flip 72%) because its payout gate needs more winning days per dollar.
Confirms the 2026-07-04 reconstruction (break-even λ ≈ −1) on real tick data
with real payout mechanics. Profit remains structural; the edge is upside.

## 3. Buy-100-tickets tables (per firm, one year, drive, net booking)

Of every 100 evals purchased:

| | Topstep | Lucid | Tradeify | Apex (30pt eval) |
|---|---|---|---|---|
| pass eval | 46.9 | 46.9 | 53.0 | 31.4 (**see §5 — 40.7 at 31pt**) |
| ≥1 payout | 20.9 | 20.9 | 24.5 | 16.3 |
| ≥2 payouts | 19.9 | 19.9 | 23.2 | 9.8 |
| ≥3 payouts | 7.6 | 7.6 | 9.0 | 7.3 |
| ≥4 payouts (full for TS/LU/TR) | 7.5 | 7.5 | 8.9 | 6.3 |
| ≥5 / ≥6 payouts | — | — | — | 5.9 / 5.4 |
| exactly 0 / 1 / 2 / 3 / 4(+..) | 79/1/12/0/8 | 79/1/12/0/8 | 76/1/14/0/9 | 84/7/3/1/0.4/0.4/5 |
| mean net per ticket | **+$745** | +$732 | +$874 | +$683 |
| median per ticket | −$85 | −$98 | −$99 | −$39 |
| **100-ticket year: mean** | **+$74.3k** | +$73.0k | +$87.9k | +$68.0k |
| year p5 / p50 / p95 | +45.5k / +73.6k / +105.5k | +44.2k / +72.3k / +104.2k | +56.9k / +87.1k / +121.1k | +35.3k / +67.1k / +103.6k |
| mean days to finish (paying tickets) | 20 | 20 | 23 | 69 |

Why "exactly 1" and "exactly 3" payouts are so rare (TS/LU/TR): cycles
alternate nuke-first (cycles 1,3) / flips-only (cycles 2,4). After payout #1
the flips-only cycle completes ~90% of the time (flip WR 90.5%) → accounts
rarely die on an odd cycle; the re-nuke cycle (28.6% per try) is where they
die → the mass sits at exactly-2 and exactly-4. That shape is mechanics, not
a bug.

Caveats: tickets are modeled independent — same-day drive correlation means
the true year p5 is worse than shown (variance understated; EVs unaffected).
Pass rates omit the final-day-target-shrink refinement (official Topstep pass
42.4% vs 46.9% here); relative comparisons unaffected. Tradeify's pass
excludes rare 40%-rule violations near $3,0xx totals (narrow window,
epsilon-rare at 0.8×).

## 4. Tradeify eval mechanics (the day-3 question)

At 0.8× the follower books $1,240/day off the leader's $1,550 eval days
(15.5pt × 5 minis; × 0.8 = 4 minis). Two wins = $2,480 < $3,000 — meanwhile
its 2-day leader passes and stops trading. **Day 3: the copier plan remaps
the follower to any still-live eval leader at the SAME 0.8× ratio**
(`copier_plan.py` "ride the eval pack"; MULTI_FIRM §4). Day-3 win books
another full $1,240 → $3,720 total, and the 40% rule is satisfied
automatically: best day $1,240 / $3,720 = 33.3% ≤ 40%. **No micro switch, no
ratio change, no $600 fine-tuning** — eval overshoot is paper (the firm keeps
it), so precision costs nothing. Only after ITS OWN pass does the multiplier
go to 1.0× (a 0.8× flip would gross $136 < the $150 win-day bar) and the
account waits for a fresh Express leader to clone for life.

## 5. Apex bracket audit

**Flip $325 vs "why not $260–270":** two separate bars matter — the win-day
minimum is **$250 NET**, and commissions eat ~$7/mini RT (doc's "~$20/mini"
looks overstated; operator to confirm actuals). Frictionless EV on real data:

| Flip target | Net/day | Margin over $250 bar | EV/ticket | days-to-finish |
|---|---|---|---|---|
| 13.00pt $260 | $253 | **$3 ≈ 0.6 ticks** | +$1,024 | 80 |
| 14.00pt $280 | $273 | $23 ≈ 4.6 ticks | +$986 | 74 |
| 16.25pt $325 (locked) | $318 | $68 ≈ 13.6 ticks | +$889 | 67 |
| 17.50pt $350 | $343 | $93 | +$854 | 58 |

Smaller flips ARE higher-EV in a frictionless model — but a $260 flip sits
0.6 ticks above disqualification: ONE tick of copier slippage on the follower
fill turns the win-day into a non-qualifying day (the top un-modeled risk in
MULTI_FIRM §3). $325 buys ~13 ticks of slippage margin and a 13-day-faster
payout cycle for ~$135/ticket of frictionless EV. **Verdict: $325 is a
defensible robustness choice; $280 (4–5 ticks margin, +$97 EV) is the
rational middle IF follower fills are confirmed within ~2 ticks.** (The
documented "$350 WR cliff" is actually a smooth ~1.2pp/1.25pt decline on
264 days — the real argument for $325 is margin + cycle speed, not a cliff.)

**Nuke "$1,250 vs $1,300":** not a discrepancy — the TARGET is $1,300 gross
(32.5pt × 2 × $20); **$1,250 is what it books NET** of ~$50 commissions. The
32.5pt choice itself is EV-flat against alternatives (27.5→+882, 30→+891,
32.5→+889, 35→+838): nothing to change.

**⚠ FINDING — the 30pt one-day eval nets SHORT of $3,000.** 30pt × 5 × $20 =
$3,000 **gross**; at any realistic commission the balance lands at ~$2,955–
2,965 — a one-day pass is arithmetically impossible, which drags the real
pass rate to 31.3% (vs 42.1% frictionless) and costs ~**$215/ticket**:

| Signal eval bracket | pass @$7 comm | EV/ticket |
|---|---|---|
| 30/10 (current locked) | 31.3% | +$674 |
| **31/10 (nets ≥$3,000)** | **40.7%** | **+$889** |

**Recommendation: change the `apex-eval` signal bracket 30.0 → 31.0pt**
(engine.SIGNAL_PLAN_TEMPLATES + docs) after confirming Apex's actual NQ
round-trip cost. Not applied — it changes a research-locked constant;
operator sign-off required. (Topstep's 15.5pt eval already carries the
commission margin: 2×$1,550 = $3,100 gross ≈ $3,030+ net ✓.)

**Retire at 6 vs 4** (31pt eval, locked brackets): +$889 vs +$668 per ticket
(+33%) — the 2026-07-06 six-payout decision is confirmed profitable; each
extra harvest cycle is nearly pure EV once the machine is running.

## 6. Apex pricing: bundle vs upfront

Cost per ticket: bundle = $39 + pass×$139; upfront = $109 flat.
Crossover at pass = 70/139 = **50.4%**.

| Eval pass rate | Bundle E[cost] | vs $109 upfront |
|---|---|---|
| 31.4% (current 30pt bracket) | $82.69 | bundle saves $26.31 |
| 40.7% (31pt bracket) | $95.57 | bundle saves $13.43 |
| 50.4% | $109 | exact wash |

**Verdict: take the $39+$139 bundle at any realistic pass rate.** The real
pass is ~40%, not ~50% — the one-shot intuition overestimates because a
one-day 30–31pt win is only a ~30% event and the trailing MLL allows ~2
lives, not unlimited retries. Either way the difference is 2–4% of ticket EV
— a genuine near-wash, bundle slightly ahead, exactly as MULTI_FIRM §3
concluded ("crossover at 50.4%").

## 6b. Addendum (same day): operator decisions & the Tradeify finisher

Decision runs (scratchpad `day3_and_apex.py`, 10–30k paths):

**Tradeify day-3 finisher.** The copier can only scale SIZE (followers mirror
the leader's exits), so a smaller finish day has the same win probability but
a smaller red day — more retries above the trailing floor:

| Policy | eval pass | ~EV/ticket |
|---|---|---|
| flat 0.8× (old) | 52.6% | +$867 |
| **dynamic mini finisher (implemented)** | **56.0%** | **+$929** |
| MNQ cross-copy finisher (~0.1-mini grain) | 52.1% | +$858 — REJECTED |

MNQ loses its granularity edge to micro commissions (~$2/micro RT ≈ 3× a
mini's cost per notional) plus tighter consistency margins. Implemented in
`copier_plan._eval_scale`: minis = ceil((max($3,000, best/0.40) + $60 −
equity) / $303), multiplier = minis/5, never above the firm scale; emits
SET_MULT "eval finisher" lines. Lucid (1.0×) is untouched — pure lockstep.

**Also evaluated and REJECTED: a dedicated Tradeify-finisher SIGNAL channel**
(operator proposal 2026-07-06: a practice-account leader firing a small-target
/ full-$1,000-stop bracket for day-3 evals, escaping the copier's
same-price-levels constraint). The mechanism works as intended — a $720/$1,000
bracket finishes 64.6% of days vs the copied bracket's 50% — but the failed
finisher burns $1,000 of the ~$2,000 trailing room (2 retry lives) where the
3-mini copied finisher burns $621 (3 lives). Die-before-finish: ~12.5% for
BOTH flat and signal finisher (the higher WR exactly offsets the bigger red
day) vs ~6% for the mini finisher. Measured over 40k paths, all sizings:

| day-3 policy | pass | ~EV/ticket |
|---|---|---|
| flat 0.8× | 52.9% | +$881 |
| **mini finisher (implemented)** | **56.2%** | **+$944** |
| signal 9/12.5 @4m ($720/$1,000) | 54.9% | +$920 |
| signal 9/10 @4m ($720/$800) | 55.1% | +$923 |
| signal 11.25/12.5 @4m ($900/$1,000) | 54.0% | +$902 |
| signal 18/25 @2m ($720/$1,000, wider points) | 55.2% | +$925 |

Retry lives compound geometrically; per-day win probability only enters
linearly — the extra life wins. Bonus: no extra practice-account channel or
copier complexity needed.

**Apex levels re-locked** (`engine.SIGNAL_PLAN_TEMPLATES`): eval 30 → 31.5pt
(pass 31.3% → 43.3%, EV +$680 → +$941 at $7 comm; robust at $20), flip
$325 → $285 = 14.25pt (EV +$941 → +$1,011, margin 5.6 ticks at $7 comm,
still above the bar at $20; operator accepts the slippage risk).
`analytics.MODEL_WR` updated to the measured WRs of the new brackets
(flip .794/.844, eval .222/.295).

## 7. Consistency sweep (code vs docs vs live config)

- `store/firms.py` ↔ MULTI_FIRM §2 table: tickets 85/98/99/39+139, DLLs
  1000/1200/none→(sim 1000)/1000, consistency .5/.5/.4/none+.5 funded,
  win-day 150/150/150/250, gate 2600/$1,500 request, caps — **all match**.
- `engine.SIGNAL_PLAN_TEMPLATES` ↔ gitbooks/accounts.md brackets — match.
- Live settings (data/users/1/settings.json) ↔ STRATEGY.md locked config —
  match (eval_stop_pts=10.0 confirmed fixed).
- `analytics.MODEL_WR` ↔ fresh measurement — within ~2pp everywhere (§1).
- Soft doc corrections: "~$20/mini RT" commission (likely ~$7–9; only
  materially affects the §5 eval finding), "$350 WR cliff" (smooth decline).
