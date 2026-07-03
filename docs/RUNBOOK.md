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
   Apex cohorts: only buy from banked payouts (the gate reason says so).

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
