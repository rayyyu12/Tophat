# Multi-Firm Build Plan — Implementation Stages

> How we build what `docs/MULTI_FIRM_PLAN.md` designed. Each stage is independently
> shippable, tested, and maps to a rollout gate (PLAN §6). No stage starts coding
> until the previous stage's exit criteria are met. Conventions throughout: dataclass
> + JSON stores with `atomic_write_text`, `merge_save` for concurrent safety, per-owner
> broker pool, pytest with `TOPHAT_DATA_DIR` isolation, single-file `index.html` UI
> using the existing ember theme tokens.

---

## Stage 0 — Pre-flight (no code)

1. Settings → `eval_stop_pts`: **9.5 → 10.0** (locked model, PROBABILITY.md §0).
2. Manual **Execute** on the practice account during the entry window; verify in the
   debug log: `PLACE` → `PLACED order_id` → working-order/position lines (fill price,
   bracket prices) → next-day `CLOSE` reconcile. This is the only untested live path.
3. Confirm with Tradecopia/firms: second practice account?, Apex consistency window,
   inactivity windows. (Answers refine constants; they don't block Stage 1–2 code.)

**Exit:** one verified live bracket round-trip on practice; config matches the model.

---

## Stage 1 — Firm profiles (`tophat/store/firms.py`)

The engine is already fully parameterized by `AccountConfig`; firms become named
parameter packs plus the *follower-accounting* fields the engine doesn't know yet.

```python
@dataclass(frozen=True)
class FirmProfile:
    key: str                      # "topstep-50k" | "lucid-50k" | "tradeify-50k" | "apex-50k"
    label: str
    ticket_cost: float
    activation_cost: float        # 0 topstep ($85 all-in) / 0 lucid,tradeify / 139 apex
    dll: float | None             # None = no DLL (tradeify)
    trailing: float               # 2_000 everywhere (today)
    eval_target: float            # 3_000
    eval_consistency: float | None    # 0.5 / 0.5 / 0.4 / None
    eval_min_days: int
    win_day_min: float            # NET day P&L that counts: 150 / 150 / 150 / 250
    payout_style: str             # "half-profit-cap" (TS/LU/TD) | "apex-gate"
    payout_cap: float             # 2_000
    apex_min_balance: float = 0.0     # 2_600
    apex_consistency: float = 0.0     # 0.50
    payout_request: float = 0.0       # 1_500 locked (apex)
    copier_scale_eval: float = 1.0    # 1.0 / 1.0 / 0.8 / n.a.(native)
    max_funded: int = 5               # 20 apex
    max_total: int | None = None      # 10 lucid
```

- Ship the four profiles as code constants (not JSON — they're research-locked);
  `data/firms.json` overlay only if the operator needs to tweak without a deploy.
- Unit tests: profile invariants (stop = half trailing; $325 flip nets ≥ win_day_min
  for apex; $170 flip nets ≥ 150 for the clones).

**Exit:** profiles importable; no behavior change anywhere else. (Small PR.)

---

## Stage 2 — Mirror accounts (`tophat/store/mirrors.py`, `tophat/services/mirror_sync.py`)

The core new concept: a **mirror** is a follower account TopHat models but never trades.

**Store** (`data/mirrors.json`, atomic writes, `merge_save`-style partial saves):

```python
@dataclass
class MirrorAccount:
    mirror_id: str            # "lucid-01" — operator-chosen slug, immutable
    firm: str                 # FirmProfile.key
    account_number: str       # the firm's real account id (manual entry)
    alias: str = ""
    leader_id: int | None     # paired Topstep account_id (None = unpaired/waiting)
    multiplier: float = 1.0   # copier scale (0.8 tradeify eval; set 1.0 at funding)
    channel: str = ""         # apex only: "nuke" | "flip" | "eval" | ""
    phase: str = "eval"       # eval | funded | passed | blown | retired | waiting
    state: AccountState       # reuses the engine dataclass (equity, peak, win days…)
    win_days: int = 0         # qualifying days this window (firm's win_day_min)
    window_profit: float = 0.0
    best_day: float = 0.0     # apex consistency tracking
    last_verified: str = ""   # YYYY-MM-DD of last manual balance sync
    enabled: bool = True
```

**Outcome propagation** (`mirror_sync.apply_leader_outcome`): hook into the existing
reconcile path in `server/service.py run_session` — when a leader reconciles
win/loss/flat, every mirror with `leader_id == aid` books the same outcome scaled by
`multiplier`, then advances its OWN accounting per its FirmProfile:
- net day P&L = scaled bracket dollars minus the commission buffer;
- qualifying-day check against `win_day_min` (NET — the $250 landmine);
- trailing floor / lock-at-start, blow detection;
- eval pass check (target + firm consistency);
- payout eligibility (`half-profit-cap` vs `apex-gate` with min-balance +
  5 qualifying days + 50% best-day window rule); eligible ⇒ `payout_ready`,
  surface in the payout queue (never auto-advance — payouts are manual at the firm).

Signal-account channels (Stage 3) propagate the same way: mirrors on a channel book
the channel's outcome instead of a paired leader's.

**Manual entry & sync:** CRUD API (`/api/mirrors` GET/POST/PATCH/DELETE) +
`sync_balance` patch (sets equity + `last_verified`, mirroring the existing
account-lifecycle editor). New mirrors default to a fresh account at the firm's
starting state, exactly as the operator requested.

**Hazard engine** (`mirror_sync.hazards()`): pure function → list of warnings:
- "within one stop of floor — tomorrow's copied {label} loss blows it"
- "eval passed — unmap from copier before next fire"
- "payout eligible — request at the firm, then Mark Paid"
- "win day netted ${x} < ${min} — did NOT qualify (slippage)" (from manual sync deltas)
- "inferred state stale — last verified {n} days ago" (n > 7)

**Tests:** propagation math per firm (incl. the $2,470 < $2,600 apex cycle case,
tradeify 3,750 rule at 1:1 vs 3,000 at 0.8×, net-based qualifying), blow/lock
edges, payout eligibility, hazard triggers. Target ~25 new tests.

**Exit:** mirrors trackable end-to-end in the API with correct per-firm accounting;
rollout Stage 2 (Lucid live) can begin on the API alone before the UI lands.

---

## Stage 3 — Signal accounts (`tophat/services/signals.py` + small engine addition)

Practice/disposable accounts that fire **their own plan** (Apex brackets) for the
copier to distribute. TopHat can already place brackets on practice accounts (manual
path); this stage lets the automation do it on designated accounts only.

- Registry entry gains `signal_plan: str` ("" | "apex-nuke" | "apex-flip" | "apex-eval").
- New engine plans (constants from PLAN §3): apex-nuke 32.5/25 @2m, apex-flip
  16.25/50 @1m, apex-eval 30/10 @5m. `plan_for_signal(plan_key, drive)` beside
  `decide()` — signal accounts bypass the eval/funded state machine entirely.
- `run_session`: accounts with a `signal_plan` fire that bracket at their entry time
  (nuke/eval 09:45; flip channel time configurable), guarded by the same
  `last_fire_date` / grace-window / hedge-guard machinery. The `is_practice` skip
  gets an exception for accounts explicitly marked as signals (explicit opt-in only).
- Channel days are decided by the solver (Stage 4): e.g. intake days repurpose the
  nuke leader to `apex-eval` — the signal_plan is set per day by the plan, not
  hand-edited.

**Tests:** signal fire path (practice + signal fires; practice alone still skipped),
bracket math for the three plans, one-position-per-account conflict guard (a signal
account gets exactly one plan per day).

**Exit:** a practice account auto-fires an Apex bracket in mock mode end-to-end;
rollout Stage 4 canaries can start.

---

## Stage 4 — The nightly solver + Copier Plan (`tophat/services/copier_plan.py`)

Deterministic function: `build_plan(leaders, mirrors, settings, today) -> CopierPlan`.
Pure — same inputs, same plan; fully unit-testable. Four blocks (PLAN §5):

1. **Replenish**: per-firm `buy = max(0, target − live_evals)` with caps
   (lucid `min(7, 10−funded)`; apex cohort gate `PAs + 0.47×evals < 16` — the
   payout-funded condition was dropped 2026-07-09: cohorts buy whenever slots clear).
2. **Pair**: apply events — leader passed (remap tradeify evals to the youngest live
   eval leader), mirror passed (lucid twin activates; tradeify → `waiting` queue),
   fresh Express appeared (pop one waiting tradeify, pair for life, multiplier→1.0),
   leader died/retired (orphan mirrors → re-queue or mark), payout taken (unmap until
   Mark Paid).
3. **Schedule**: existing Topstep scheduler untouched; Apex mirror scheduling —
   intake ≤2/day on the eval channel; **1 nuke slot/day** rotation over nuke-mode PAs
   (longest-since-nuke first); everyone else flip-mode.
4. **Emit** `CopierPlan`:

```python
@dataclass
class PlanLine:
    action: str        # MAP | UNMAP | MOVE | SET_MULT | SET_CHANNEL | BUY | REQUEST_PAYOUT
    mirror_id: str
    detail: str        # human text incl. leader/channel/multiplier
    reason: str        # why (leader passed / payout ready / cycle reset …)

@dataclass
class CopierPlan:
    date: str
    lines: list[PlanLine]
    channels: list[dict]     # channel -> bracket -> follower count
    buys: list[dict]
    hazards: list[dict]
    applied_at: str = ""     # operator confirmation timestamp
```

- Persist to `data/copier_plans/{date}.json` (atomic). `POST /api/copier-plan/apply`
  stamps `applied_at`; propagation for signal channels only activates for mirrors the
  applied plan says are mapped (an unapplied plan = yesterday's mappings still rule —
  this keeps the inferred world honest when the operator skips a day).
- Diff logic: plan lines are generated by comparing yesterday's applied mapping to
  today's computed mapping — the emitted list IS the edit list for Tradecopia.

**Tests:** golden-file plans for scripted fleets (pass/blow/payout/orphan days),
determinism (same state ⇒ identical plan), cap enforcement, diff minimality
(no-op day ⇒ zero lines). Target ~20 tests.

**Exit:** 3 consecutive live days where the generated plan matches what the operator
would have done by hand (shadow mode — generate but don't rely).

---

## Stage 5 — UI restructure (single-file `index.html`, existing ember theme)

Design decisions (ui-ux-pro-max review, applied to the existing system — the current
warm-dark token set stays; no new palette):

**Navigation** — sidebar gains one item and one rename; router stays `.nav[data-page]`:

| Order | Page id | Label | Icon (Lucide) | Content |
|---|---|---|---|---|
| 1 | `page-ops` | **Operations** | `clipboard-list` | Copier Plan checklist, mappings, buy list, payout queue, hazards |
| 2 | `page-trading` | **Trading** | `candlestick-chart` | the current dashboard (drive, per-owner tables, execute) |
| 3 | `page-accounts` | Accounts | (existing) | + mirror entry/edit (new section) |
| 4 | `page-settings` | Settings | (existing) | + firm-profile overlay card |

Operations is the new default landing page **only when mirrors exist**; otherwise
Trading stays default (don't confront a Topstep-only user with an empty ops page —
`empty-nav-state` rule: show the page but explain + CTA "Add your first mirror").

**Operations page layout** (desktop 2-column, stacks at <1024px):

- **Left column — "Today's Copier Plan"** card: date header, N-line diff list.
  Each line: action chip (MAP/MOVE/UNMAP color-coded by `--flip/--warn/--short` +
  icon + text — never color alone), mirror alias, human reason in `--muted`.
  Footer: single primary CTA **"Mark applied in Tradecopia"** → themed confirm modal
  (existing modal component; `confirmation-dialogs` rule) → success toast
  (`aria-live="polite"`, 4s auto-dismiss). Empty state: "No changes today — mappings
  carry over." Applied state: green check header "Applied 08:12 ET", CTA disabled
  (`disabled-states`: 0.45 opacity + no pointer).
- **Left column — "Buy list"** card (Mondays): firm, count, cost, running total in
  `--mono` tabular numerals; check-off per row.
- **Right column — "Channels & Mappings"**: one collapsible group per firm
  (Lucid / Tradeify / Apex), dense tables in the existing table idiom
  (`overflow-x:auto` wrapper — `table-handling` rule). Columns: mirror, leader/channel,
  multiplier, phase badge, inferred balance (mono, right-aligned), room-to-floor,
  last-verified (relative, warn tint > 7d). Row click → drawer with full inferred
  state + "Sync balance" inline form (visible label + helper text, validate on blur).
- **Right column — "Payout queue"**: payout-ready mirrors + leaders, amount,
  eligibility met date, "Mark paid" (confirm modal). Mirrors of the existing
  payout-ready flow.
- **Hazards strip** — full-width above both columns when non-empty: `--warn` left
  border accent cards, icon + text + affected mirror link. Never a toast (persistent
  until resolved — hazards are state, not events).

**Mirror entry (Accounts page):** "Add mirror account" form — firm select, account
number, alias, multiplier (pre-filled from firm profile, e.g. 0.8 for Tradeify eval),
leader select (or "waiting"). Labels visible, required marked, errors below fields,
submit → loading → toast (`submit-feedback`). Bulk import: textarea "one account
number per line" per firm — enough; CSV upload is overkill for ≤40 accounts.

**Conventions carried over:** ET times with viewer-local conversion via the existing
`etToLocalHHMM`; status badges reuse the Trading page's badge component (add `waiting`
= `--idle`, `payout-ready` = `--warn`); numbers in `--mono` with `font-variant-numeric:
tabular-nums`; focus rings on all interactive elements; `cursor:pointer`; 150–300ms
transitions with the existing `--ease`; no emoji icons (inline SVG, Lucide set,
consistent 18px stroke like current sidebar icons).

**WebSocket:** the existing 3s push gains `ops` payload (plan + hazards + queue) so
the Operations page live-updates like the dashboard.

**Tests:** API-level (plan/apply/mirror CRUD endpoint tests); UI is manually verified
via `preview` (desktop 1280 + 375px mobile pass, focus-visible pass, dark-contrast
spot checks — the theme is dark-only by design, matching its OLED-dashboard idiom).

**Exit:** operator runs a full live day driving Tradecopia purely from the
Operations page.

---

## Stage 6 — Hardening & ops

- **Drift alarms**: weekly sync reminders escalate to a hazard at 7 days, a red
  banner at 14. Mirror equity vs firm-dashboard mismatch > $50 on sync → "propagation
  drift" hazard prompting a full-state re-entry.
- **Fleet kill switch**: one Operations-page toggle that (a) disarms automation,
  (b) emits an UNMAP-ALL copier plan — the "firm changed rules / copier misbehaving"
  break-glass. (Rule: destructive, so it's spatially separated + double-confirm.)
- **Backups**: `data/` snapshot on server start (existing) + daily copy of
  `mirrors.json` and applied plans (30-day retention) — inferred state is the one
  thing we can't recompute from the broker.
- **Docs**: RUNBOOK.md — the operator's daily 10-minute routine, step by step.

---

## Test & rollout tie-in

| Code stage | Unblocks rollout stage (PLAN §6) |
|---|---|
| 0 | Stage 1 (armed Topstep with correct stop) |
| 1–2 | Stage 2 (Lucid live — API-only is acceptable) |
| 3 | Stage 4 canaries (Apex signal validation) |
| 4–5 | Stage 3–5 at full comfort (daily plan UX) |
| 6 | steady state |

Suite grows ~60 tests (116 → ~175). Every stage lands green with
`python -m pytest tests/`.
