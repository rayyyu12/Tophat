# TopHat Operator Runbook — the daily 10 minutes

> The multi-firm system automates everything it can reach (Topstep via API, follower
> inference, the daily solve). Three things stay manual by design: **Tradecopia edits,
> money movement, and account purchases.** This is the routine that covers them.
> Architecture/context: `docs/MULTI_FIRM_PLAN.md` · build detail: `docs/BUILD_PLAN_MULTIFIRM.md`.

## Evening (or before 09:30 ET) — apply the Copier Plan  (~10 min)

1. Open **Operations**.
2. Work the **hazards strip** first — danger items (near-floor mirrors) may mean
   unmapping an account the plan would otherwise keep trading.
3. Go line-by-line through **Today's Copier Plan** and make each edit in Tradecopia:
   - `MAP` / `MOVE` — point the follower at the named leader, at the shown multiplier.
   - `UNMAP` — remove the follower's mapping (passed evals, payout parking, nuke-queue).
   - `SET MULT` — change the follower's multiplier (0.8× Tradeify evals → 1× at funding).
   - `SET CHANNEL` — informational: the MAP line beneath it is the actual edit.
   - `ACTIVATE` — do the TopHat-side step it names (Accounts → Activate funded / Pair).
4. Click **Mark applied in Tradecopia** → Confirm. TopHat now assumes the new
   mappings are live; inference tracks accordingly. *(Skipped a line? Fix it in
   Tradecopia to match the plan — the plan is the source of truth once applied.)*
5. **Buy list** (Mondays): purchase the listed evals at each firm, then add them as
   mirrors (Accounts → Mirror accounts; paste account numbers comma-separated).
   Targets (2026-07-11 sweep, research/forecast_replenishment_sweep.py): Topstep 6
   standing **per login**, Tradeify 6, Lucid 10, Apex topped up to 8 standing while
   PAs < 16 — no payout-funded rule, fund them from wherever (operator decision
   2026-07-09). Lucid buys pause automatically once three passed twins are waiting
   for a funded slot (twin throttle loosened 1 → 3, 2026-07-11).
6. **New Topstep accounts: enable Auto OCO Brackets** (Topstep platform → account
   settings) on every fresh eval AND every fresh Express funded account. Accounts
   left on "Position Brackets" reject every API bracket (`error 2 … You must
   enable Auto OCO Brackets` — this cost three evals their day on 2026-07-09).
   Two safety nets, both Discord-only (no dashboard pill):
   - **Nightly OCO probe** (Settings → *OCO probe time*, default 22:00 ET =
     21:00 CT, Sun–Thu): places a far-below-market bracketed limit order on
     every ACTIVE account and cancels it immediately; any account still on
     Position Brackets rejects the probe and gets named in the alert. Copy
     LEADERS are skipped (a probe order would replicate to followers) — their
     OCO state is proven by their daily live fire instead. Practice accounts
     and dead-by-balance accounts (at/below the trailing floor but still
     canTrade=true at the broker) are skipped too. One probe per night,
     stamped to disk — a restart after the probe will not re-run it; a
     restart that lands past the probe time on a night it hasn't run yet
     probes on the first tick (by design, catch-up).
   - **Fire-failure alert** at entry time: a rejected fire names the account
     and the reason. The attempt is CONSUMED — one attempt per account per
     day, no retries (a later entry is off-strategy); the account trades again
     tomorrow after you fix the setting.

## After payouts land

- **Payout queue** (Operations): request the shown amount at the firm, and once it
  pays, click **Mark Paid**. Clone firms retire automatically after payout 4.
- Topstep leaders: the existing Mark-withdrawn flow on the Trading page, unchanged.

## Weekly — balance sync (~5 min)

For every live mirror: open the firm's dashboard, read the account P/L vs its
starting balance, and enter it via the sync icon (Accounts → Mirror accounts row).
Stale mirrors (>7 days) raise a hazard on their own. **A mismatch > $50 vs the
inferred figure means propagation drift** — usually a follower fill that missed a
qualifying bar; correct win_days too if the firm's dashboard disagrees.

## Monthly-ish — import new NQ tick data (~5 min + export time)

Whenever there's a fresh month of data (the Simulation page header shows the
current coverage window):

1. NinjaTrader: **Tools → Historical Data → Export** — instrument (e.g.
   `NQ 09-26`), type **Tick**, format **Text**. Repeat per contract touched.
2. `python research/reconstruction/import_ticks.py "path\to\NQ 09-26.Last.txt"`
   (accepts multiple files or a directory). New dates replace overlapping ones;
   everything else is kept, and it prints the new coverage window.
3. `git add research/reconstruction/sim_ticks_rth_1s.parquet` + commit + push —
   the cache ships in the repo, which is what puts the new days in front of
   every user and deployment. A locally running server picks the change up on
   its next read, no restart.

## Signal accounts (Apex channels)

- Designate on Accounts → *Signal channel*: practice account = `apex-nuke`
  (or `apex-eval` during cohort intake — nuke rotation pauses those days),
  disposable micro eval = `apex-flip`.
- A signal account fires its bracket daily at 09:45 ET with the drive, automation-armed,
  same guards as the fleet (one/day, grace window, hedge guard).
- When a disposable signal eval passes or blows, buy the next $39 one and re-designate.

## If something looks wrong

- **Kill it**: Settings → `auto_execute=false` disarms all automated fires; then
  unmap everything in Tradecopia. (A one-click fleet kill switch is a planned
  hardening item — until then this two-step is the break-glass.)
- **Trust order**: firm dashboard > Tradecopia > TopHat inference. Fix TopHat's
  view via sync + the mirror editor; never assume the inferred number wins.
- Every fire/reconcile/mirror booking is in `logs/tophat-*.log` (`MIRROR`,
  `PLACE`/`PLACED`, `CLOSE`, `SESSION done` lines).
- **A funded account idling all day after another blew is CORRECT**: a nuke/eval
  slot consumed by an account that blows, passes, or vanishes mid-morning stays
  consumed — the next account queues for tomorrow (one attempt per slot per
  day; re-awarding the freed slot is what double-fired two Express accounts on
  2026-07-09).
