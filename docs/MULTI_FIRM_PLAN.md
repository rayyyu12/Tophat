# TopHat Multi-Firm Expansion — Master Plan & Context

> **Purpose of this document:** the complete, self-contained record of the multi-prop-firm
> expansion — research, simulation results, locked policies, architecture, rollout schedule,
> and open questions. Written 2026-07-02 so any future session (human or AI) can pick this
> up with zero prior context. Companion simulation: `research/compare_firms.py`
> (built on `research/backtest_common.py`, same 251-day 1s RTH drive cache as everything else).
> Baseline strategy & probability context: `docs/STRATEGY.md`, `docs/PROBABILITY.md`.

---

## 0. Immediate action items (before anything else)

1. **Fix live config:** `data/settings.json` has `eval_stop_pts: 9.5` ($950). The corrected
   model (PROBABILITY.md §0, 2026-06-24) locked the eval stop at **$1,000 (10.0 pt)** — the
   $950/52% figure was a modeling artifact. Update in Settings before the next armed day.
2. **Validate the order path:** automation has still never placed a real live order
   (2026-07-02 ran clean end-to-end but only the practice account was enabled, which the
   auto path skips by design). Manual **Execute** on the practice account during the entry
   window is the validation path — it exercises PLACE/fill/bracket/reconcile logging.

---

## 1. The idea

Topstep is the only firm with an API (TopstepX/ProjectX gateway). Other firms
(Lucid, Tradeify, Apex) have no API — but **Tradecopia** (the copy-trader, confirmed
working) can copy trades from any Topstep account (real, funded, or **practice**) to
follower accounts at other firms, with per-follower multipliers.

So the architecture is **leaders and followers**:

- **Leaders** = Topstep accounts. TopHat trades them via API (existing system).
- **Followers** = accounts at API-less firms, registered manually in TopHat as
  "mirror" accounts. TopHat never places their orders (Tradecopia does) but *models*
  them: same bracket + same market day = same outcome, so follower state (balance,
  floor, win days, payout eligibility) is **inferred by outcome propagation** from the
  leader's reconciliation, with periodic manual balance sync against the firm dashboards.
- **Signal accounts** = Topstep practice account(s) (and optionally a disposable micro
  Apex eval) used as Tradecopia leaders to fire brackets that no real Topstep account
  would fire (Apex-native strategy). TopHat can already trade the practice account via
  API — manual Execute allows practice; a small code change will let designated signal
  accounts auto-fire their own plan.

Tradecopia facts (confirmed by operator):
- Works over Rithmic connections; any Topstep account (incl. practice) can lead.
- Per-follower multipliers, fully flexible; MNQ available for fine scaling.
- NQ+MNQ dual-channel per leader exists but operator prefers not to use it.
- NinjaTrader SIM accounts can **not** lead (no Rithmic connection) — ruled out.
- Mapping changes are manual; operator budget ≈ **10 min/day** of copier edits.

---

## 2. Firm rules (operator-verified 2026-07-02)

All four firms: $50K account, **$2,000 trailing MLL** (the shared binding constraint),
$3,000 eval target, ~$2,000 max payout.

| | Topstep | Lucid | Tradeify | Apex |
|---|---|---|---|---|
| Ticket (all-in) | $85 | $98 | $99 | $109, or $39/eval (5-bundle) + $139 activation on pass |
| DLL | $1,000 | $1,200 | none | $1,000 |
| Eval consistency | 50% (⇒ ≥2 days) | 50% (⇒ ≥2 days) | **40%** | **none** (1-day pass possible) |
| Funded consistency | none | none | none | **50%** |
| Winning-day minimum | $150+ ×5 | $150+ ×5 | $150+ ×5 | **$250+ ×5** |
| Payout gate | $4,000 profit → 50% capped $2k | same | same | **balance ≥ +$2,600**, request $≤2,000 |
| Account caps | 5 funded (10/mo buy limit unverified) | 5 funded, **10 total** | 5 funded | **20 funded**, evals ~unlimited |

Key subtleties discovered:
- **Qualifying/win-day minimums apply to the day's BOOKED (net) P&L.** Commissions eat
  ~$20/mini round trip. A $250-gross Apex flip nets ~$230 → **never qualifies** (sim:
  literally zero payouts, ever). A $170-gross Topstep/Lucid/Tradeify flip nets ~$150 →
  qualifies with no margin; the locked $170 target is already exactly calibrated.
- **The $1,000 stop is correct at every firm** — it's half the shared $2,000 MLL, so an
  account is always a whole number of "lives" from the floor (no fractional-room states).
  Lucid's $1,200 DLL / Tradeify's no-DLL just add slippage headroom on followers.
- Apex trailing floor locks at breakeven once peak ≥ $2,000 (same lock-at-start model
  as Topstep funded). Consistency window **assumed to reset per payout** — if it's
  actually account-lifetime, our numbers are conservative (it gets easier each cycle).

---

## 3. Simulation results (research/compare_firms.py, N=100k, 251 real days)

Method: win rates are real (target, stop) barriers resolved bar-by-bar on the 1s cache;
lifecycle Monte Carlo with intraday MLL (effective stop = min($1,000, room), win prob
recomputed at the tighter stop; two consecutive full stops from fresh = blown — verified).

### Leg win rates (drive entries)

| Bracket | WR |
|---|---|
| Eval $1,500 day (15/10pt @5 minis) | 46.6% |
| Topstep nuke $3,200 (80/25 @2m) | 27.9% |
| Topstep flip $170 (8.5/50 @1m) | 88.4% |
| Apex nuke $1,300 (32.5/25 @2m) | **48.8%** |
| Apex flip $325 (16.25/50 @1m) | 81.5% (WR cliff just above: $350→79.1%) |
| Apex 1-day eval $3,000 (30/10 @5m) | 30.2%/attempt |

### Eval pass rates (copy-mode: follower receives leader's $1,500-day bracket)

| Variant | Pass | E[days\|pass] |
|---|---|---|
| Topstep / Lucid (50%, ≥2d) | 40.1%* | 3.5 |
| Tradeify @ 1:1 (40% rule forces total ≥ $3,750) | 32.8% | 5.5 |
| **Tradeify @ 0.8× (4 minis, $1,200 days → total ≥ $3,000 suffices)** | **41.2%** | 6.3 |
| Apex copy-mode (no rule — but no benefit either) | 40.1% | 3.5 |
| **Apex NATIVE: 1-day $3,000 attempts (signal leader)** | **46.6%** | **1.4** |

*Sim omits the final-day target shrink; official corrected number is 42.4%. Relative
comparisons unaffected.

### Funded value per funded account

| Variant | E[$ withdrawn] |
|---|---|
| Topstep leader (official corrected model) | $1,450 |
| Lucid follower (lockstep clone — identical rules & trades) | $1,450 |
| Tradeify follower (random-phase join sensitivity) | ~$1,544 |
| Apex copy-mode (BROKEN: $170 flips < $250 qualifying) | $872 @ 1yr — skip |
| **Apex NATIVE, best policy (below)** | **~$2,230 @ 1yr** (E[payouts] 1.5, P(≥1) 47%, 1st ≈ day 11) |

### Apex native locked policy (winner of a 20-combination sweep + request/flip sweeps)

> **$1,300 nuke → on a miss, fire the SAME $1,300 nuke again (plain re-nuke, not a
> widened recovery bracket) → $325 flips → request $1,500 (never $2,000) → nuke again
> next cycle.**

- Plain re-nuke beats recovery-renuke ($2,210 vs $1,959): the 57.5pt recovery bracket
  wins only 34.3% and its +$2,250 best-day forces a $4,500 consistency window.
- Re-nuking each cycle beats flip-only cycles ($2,210 vs $1,794).
- $325 flips dominate ($250 = zero payouts; $275 nets $255 — one tick from
  disqualification; $350+ falls off the WR cliff).
- Request sweep: $1,000→$1,945, $1,300→$2,171, **$1,500→$2,231**, $2,000→$1,746
  (draining to a $600 cushion inflates ruin).
- **Cycle anatomy (net $):** nuke +$1,250 → 4 flips +$1,220 = $2,470 — *under* the
  $2,600 bar (gross arithmetic 1300+4×325=$2,600 misses by ~$130 of commissions) →
  5th flip → $2,775 → request $1,500 → $1,275 retained. Cycle 2 similarly needs
  nuke + 5 flips (consistency fails by $15 after only 4). **Steady state ≈ 6–8 trading
  days per $1,500 payout**, balance ratcheting ~$1,275/cycle.
- Pricing: bundle ($39 + $139 on pass, E ≈ $104) vs $109 upfront — a ~$5 wash;
  take the bundle (crossover at 50.4% pass).

### EV per eval ticket (the decision table)

| Firm | Pass | × E[$/funded] | − ticket | **EV/ticket** | ROI |
|---|---|---|---|---|---|
| Topstep | 42.4% | $1,450 | $85 | **+$530** | +623% |
| Lucid | 42.4% | $1,450 | $98 | **+$517** | +527% |
| Tradeify @0.8× | ~41% | ~$1,500 | $99 | **+$515** | +520% |
| Apex native | 46.6% | $2,231 | ~$104 | **+$930** | +894% |
| Apex copy-mode | 40% | $872@1yr | $109 | +$241 | skip |

(Topstep/Lucid rows use the official 42.4% pass; Tradeify/Apex use sim values.)

**Verdicts: Lucid yes (clone). Tradeify yes at 0.8× eval multiplier. Apex yes, native
signal architecture only — it's the best ticket in the lineup. Apex copy-mode: never.**

### Not modeled (known limitations)

Copier fill slippage (leader/follower differ by a tick or two); Apex consistency-window
definition (per-payout assumed = conservative); intra-channel correlation means EVs are
right but variance is understated (followers on one channel are one bet at N× stake,
not N draws); firm inactivity rules (operator says Tradeify fine; multi-day idles are
part of the design — verify each firm's max-idle window once).

---

## 4. Per-firm operating design

### Lucid — the pure clone
- Eval: pair 1:1 with the Topstep eval pack (any live eval leader — they all fire the
  same bracket at 09:45; identical rules ⇒ passes/blows the **same day** as its leader).
- Funded: its funded twin is born the same day as the leader's Express account →
  **lockstep for life** (same nukes, flips, payouts, death). Zero decision-making.
- Cap management: 10 total. Standing target: `evals = min(7, 10 − funded_now)`.

### Tradeify — one extra day, then a clone
- Eval at **0.8× multiplier** (4 minis; $1,200 wins / $800 stops). Needs 3 clean wins
  (vs leader's 2), so it always outlives its first leader: when that leader passes,
  **remap to any still-live eval leader** (pack-follow) for the remaining day(s).
- On passing: **set multiplier to 1:1** (a 0.8× flip grosses $136 < the $150 win-day bar),
  then **wait flat for the next fresh Topstep Express account** (~2–4 trading days) and
  pair for life → lockstep clone thereafter. Never free-run against a mid-cycle leader.

### Apex — native signal channels
An Apex account's required bracket depends only on its **mode**, not its cycle position:
- **Channel A — NUKE** (32.5/25pt @ 2 minis): serves whichever PA holds today's nuke slot.
- **Channel B — FLIP** (16.25/50pt @ 1 mini): serves every flip-mode PA (identical bracket
  regardless of cycle drift).
- **Channel C — EVAL** (30/10pt @ 5 minis): only during cohort intake.

Leader budget (operator constraints: no NQ+MNQ dual-channel; NT-sim leaders impossible):
1. Practice account #1 → Channel A (nuke).
2. **Disposable Apex micro eval → Channel B (flip):** leader trades 1 micro (±$33–65/day,
   meaningless P&L); followers map micro→minis with the multiplier. It slowly grinds
   toward its own eval target (~+$44/day expected) — when it passes or blows, buy the
   next $39 eval. A signal channel for ~$39 per ~2–3 months.
3. Channel C: a second Topstep practice account if creatable; otherwise **time-share
   Channel A** — on intake days the nuke rotation pauses (nuke-mode PAs idle in queue,
   costless, exactly like Topstep's one-nuke-a-day queue).

**Apex staggering (operator requirement — never all 20 at once):** mirror the Topstep
discipline. Intake **2 evals/day** (cohort of 10 = one week of intake); funded PAs take
turns on the nuke slot **1/day rotation** (longest-since-last-nuke first, pending
recoveries priority — same scheduler logic as Topstep); flip-mode PAs share Channel B.
Honest limitation: with one flip channel, all flip-mode PAs fire *at the same time* —
staggering flip *times* requires one leader per entry time. More practice accounts →
more flip channels at staggered times (09:45 / 10:15 / …). Until then, accept same-time
flips (day-level correlation via shared drive direction dominates anyway).

---

## 5. The deterministic control system ("the brain")

Operator's requirement: a system that always knows — deterministically — who leads, who
follows whom, what to buy, and what fires tomorrow. Design: a **nightly solver** over
state TopHat already tracks (leaders via API; followers via outcome-propagated mirrors).
Same inputs ⇒ same outputs. Four rule blocks, run in order:

1. **REPLENISH** (Mondays, or daily check): for each firm,
   `buy = max(0, E_target − live_evals)` subject to caps and the budget gate (§6).
   Standing targets: Topstep 10 · Lucid min(7, 10−funded) · Tradeify 10 ·
   Apex: new 10-eval cohort when `PAs + 0.47 × live_evals < 16` (keeps E[PAs] under the
   20 cap). Eval counts are static by construction — funded counts float with
   probability, exactly as the operator framed it; caps absorb the upside.
2. **PAIR**: apply the pairing rules (§4). Events that trigger repairs: leader passes
   (Tradeify eval remap), follower passes (Tradeify wait-for-fresh queue; Lucid twin
   birth), leader dies/retires (orphan → re-queue), payout taken (unmap until withdrawn).
3. **SCHEDULE**: existing Topstep scheduler (2 eval slots/day, 1 nuke/day, staggered
   flips) + the Apex mirror of it (2 intake/day, 1 nuke slot/day rotation, flip channel).
4. **EMIT** three artifacts:
   - **Buy list** ("Monday: 2× Topstep, 1× Lucid, 10× Apex bundle").
   - **Copier Plan diff** — the ~5–10 line daily Tradecopia edit (MOVE/MAP/UNMAP/SET
     lines with account, leader, multiplier). Operator applies in ~10 min, checks a box;
     TopHat assumes applied and propagates outcomes accordingly.
   - **Fire plan** — what each leader/signal account fires tomorrow (signal accounts
     auto-fire via the existing automation once signal-account support lands).
   Plus **hazard warnings** from inferred mirror state ("APEX-12 has $400 room — a nuke
   loss tomorrow blows it; consider unmapping").

Drift correction: weekly manual balance sync per follower (existing sync-balance edit);
mirrors are flagged "inferred — last verified {date}".

---

## 6. Rollout schedule & bankroll (risk budget: $5–10k, NOT $25k)

Principle: **stage entries behind evidence gates, and after Stage 4 expand only from
cumulative payouts.** Staggering entry in time converts one big correlated bet into
sequential smaller bets with abort options.

| Stage | When | What | Cost | Gate to proceed |
|---|---|---|---|---|
| 0 | now | Fix `eval_stop_pts` 9.5→10.0; manual-Execute validation on practice | $0 | order path proven (fills, brackets, reconcile) |
| 1 | wk 1 | Topstep only, 10-eval pipeline (already running) | ~$850 | ≥1 week of clean automated fires + reconciles |
| 2 | wk 2–3 | + Lucid mirrors (≤7 evals) — zero new strategy risk (clone) | ~$690 | copier mechanics proven: maps, multipliers, outcome inference matches firm dashboards for 2+ wks |
| 3 | wk 4–5 | + Tradeify 10 evals @0.8× | ~$990 | Stage-2 evidence + first Topstep funded payouts flowing |
| 4 | wk 6+ | + Apex cohort #1: 2 canary accounts first ($78), then 10-eval cohort staggered 2/day | ~$1,040 + ~$650 activations | signal channels proven on canaries; cumulative payouts ≥ Stage-4 cost |
| 5 | wk 9+ | Apex cohort #2 toward the 20-PA cap; scale all pipelines | payout-funded only | trailing 4-wk P&L positive |

Worst-case out-of-pocket through Stage 4 ≈ **$4.2k**; with replenishment through a bad
stretch, ceiling ≈ **$8–10k** — inside the risk budget, with three abort points where a
failing assumption stops the spend. (The old "$25k to survive 5 bad batches everywhere"
scenario only exists if everything launches simultaneously — which this schedule forbids.)

Steady-state economics at full deployment (long-run expectations; arrives in lumpy,
correlated draws): spend ≈ $1.0–1.2k/wk on replenishment tickets; expected value created
≈ **$5–6k/wk** across ~2.9 Topstep + ~2 Lucid + ~2.9 Tradeify + ~2.3 Apex tickets/wk at
the EV/ticket figures in §3. Variance is high and correlated — judge performance on
4-week windows, not days.

---

## 7. TopHat build scope (the integration, in order)

1. **Firm profiles**: generalize `AccountConfig` into named rule packs (topstep-50k,
   lucid-50k, tradeify-50k, apex-50k: consistency %, qualifying-day minimum, payout gate,
   DLL). Engine `decide()` is already fully parameterized.
2. **Mirror account store**: manual registry (synthetic id, firm profile, paired leader,
   multiplier, own AccountState) + outcome propagation from leader reconcile + manual
   balance-sync UI (exists) + "inferred — last verified" flag.
3. **Signal-account support**: designate practice/disposable accounts as signal leaders
   with their own fire plan (Apex brackets), auto-fired by the existing automation.
4. **The nightly solver + Copier Plan diff UI** (§5) with apply-checkbox and hazard warnings.
5. **Dashboard**: per-firm groups, pod view (leader + follower rows), payout-queue panel.

## 8. Open questions

- Can a second (third…) Topstep practice account be created? (→ more Apex channels,
  staggered flip times, no intake/nuke time-sharing.)
- Apex consistency window: per-payout-window (assumed, conservative) or account-lifetime?
- Firm inactivity rules: max idle days at Lucid/Tradeify/Apex (multi-day idles are
  designed in: Tradeify wait-for-fresh, Apex nuke queue).
- Topstep 10-accounts/month purchase limit: real? (Operator bought 20 last month fine.)
- Tradecopia mapping import/export or API? (Would shrink the 10-min daily edit further.)

## 9. Reproducing the numbers

```
python research/compare_firms.py          # firm comparison, policy sweeps (N=100k)
python research/probability_intraday.py   # corrected Topstep baseline (N=200k)
```
Both need `data/cache/nq_rth_1s.parquet` (local-only). `research/` is untracked by design.
Key constants to revisit if firm rules change: `compare_firms.py` header block.
