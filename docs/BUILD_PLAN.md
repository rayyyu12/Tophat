# TopHat NQ — Build & Cleanup Plan

> Companion to [STRATEGY.md](STRATEGY.md). Covers: what exists today, codebase cleanup,
> the move from CLI/TUI to a hosted **web dashboard**, full lifecycle **automation**, the
> **hedge-guard** layer for staggered flips, and execution mechanics (drive direction + brackets).
> Planning only — no code in this pass.

---

## 1. What already exists (so we don't rebuild it)

Three layers, all present and working:

- **`research/`** — the analysis code (data loader, backtests, Monte Carlos). Dev-only; never ships to the live server.
- **`tophat/`** — the live application package:
  - `engine.py` — phase state machine + DLL/trailing-drawdown guards (the decision core: `decide(cfg, state, drive)`).
  - `streaming/drive.py` — **`DriveTracker`**: locks the 09:30–09:45 ET direction at 09:45 from streamed quotes. *(Answers the "how do we get drive direction fast" question — see §6.)*
  - `streaming/manager.py` — ProjectX **SignalR** market hub (NQ quotes) + user hub (account/order/position/trade pushes). Real-time, no REST polling.
  - `broker/projectx/` — REST auth, accounts, NQ contract resolve, **bracket orders** (`brackets.py`: market entry + OCO target/stop as *tick offsets* → ProjectX places them relative to the actual fill).
  - `store/` — per-account lifecycle state persisted to JSON (`states.py`), account registry / enable-flag (`registry.py`).
  - `services/runner.py` — per-day orchestration: load states → for each enabled account `decide()` → place bracket. Nukes gated behind `confirm_nukes`.
  - `cli/` + `ui/app.py` — Typer CLI and a **Textual terminal TUI** (`run_dashboard`). *This is the part being replaced by the web dashboard.*
- **Root `*.py`** — thin **shims** (`backtest.py` → `research.backtest`, `probe.py`/`runner.py` → `tophat` CLI, etc.). Convenience only.

**Key implication:** the entire `data/` + `research/` tree is dev-only. The live server needs only the
`tophat/` package + config + state store. It needs **none** of the raw ticks, parquet caches, or backtests.

---

## 2. Reconcile the engine to the locked spec ✅ DONE

`tophat/engine.py` `AccountConfig` now matches [STRATEGY.md](STRATEGY.md): `nuke_target_dollars=3_200`,
`flip_target_dollars=170`, a separate `flip_contracts=1`, a configurable `payout_cap`, and a day-2
recovery bracket (`nuke_bracket_pts(attempt)` widens to `nuke + dll` on the retry; `on_funded_result`
credits the actually-placed bracket). The original gaps were:

| Field | Current | Target | Note |
|---|---|---|---|
| `nuke_target_dollars` | `4_000` | **`3_200`** | 80pt/25pt at 2 minis. |
| `flip_target_dollars` | `150` | **`170`** | 8.5pt. |
| flip contracts | uses `funded_contracts` (2) | **1 mini** | Engine currently shares one `funded_contracts` for nuke *and* flip — needs a separate `flip_contracts`. |
| day-2 nuke | re-places same bracket | **widen to recovery target** (105pt/$4,200) | Engine doesn't model recovery; after a day-1 loss it under-recovers. |

Until these match, the backtested win rates do not transfer to live.

---

## 3. Codebase cleanup plan

Marked for action; nothing deleted in this pass. Suggested: move "archive" items to `research/archive/`
rather than hard-delete, so the analysis history is preserved.

| Item | Action | Reason |
|---|---|---|
| `data/*.txt` (~6.3 GB raw ticks) | **Exclude from server / git**; keep locally or cold-archive | Source data; only needed to rebuild/extend caches. Add `data/*.txt` to `.gitignore`. |
| `data/cache/*.parquet` (~130 MB) | **Exclude from server / git** | Backtest-only; live uses streaming. |
| `research/session_loader.py`, `research/backtest_flip_sessions.py`, `data/cache/nq_globex_1s.parquet` | **Archive** | Overnight/globex-session exploration. Dead-end vs the locked RTH 09:45 drive (nothing else imports them). |
| Root shims (`backtest.py`, `monte_carlo.py`, `pipeline.py`, `engine.py`, `backtest_*.py`, `run_campaign_compare.py`, `monte_carlo_*.py`) | **Trim to a minimal set** | Convenience redirects. Keep `tophat.py` (entry point); run research via `python -m research.X`. ⚠️ The master plan links to root paths — update those links if shims are removed. |
| `ninjatrader/Strategies/*.cs` | **Archive or keep as reference** | Alternate NT8 execution path. Not used if ProjectX is the chosen broker. |
| `data/account_states.json`, `data/account_registry.json` | **Keep, then migrate to DB** (§4) | Live runtime state. JSON is fine for one box; a DB is better for a server + dashboard. |
| `docs/CLI.md` | **Keep, mark "legacy CLI"** | Superseded by the web dashboard but still valid for headless ops. |
| `monte_carlo.py` (research, "old bug" model) | **Keep but label** | Superseded by `monte_carlo_scenarios.py` / `run_3200_campaign.py`. Header already says so. |

---

## 4. Target architecture: hosted server + web dashboard

Goal: a clean web dashboard, fully automated, minimal human input. You buy accounts; the system
auto-discovers them via ProjectX, runs the lifecycle, and surfaces only what needs a human.

```
┌─────────────── Web Dashboard (browser) ───────────────┐
│  accounts table · enable/disable · params · schedule  │
│  live: phase, payout cycle, equity, today's plan       │
└───────────────────────┬───────────────────────────────┘
                        │ REST + WebSocket (server push)
┌───────────────────────┴───────────────────────────────┐
│  Backend API service (FastAPI)                         │
│   • account registry + lifecycle state (DB)            │
│   • scheduler (fires the daily plan at 09:45 ET …)     │
│   • orchestrator = tophat.services.runner (reused)     │
│   • hedge-guard + flip stagger (§5)                    │
└───────────┬───────────────────────────┬───────────────┘
            │                           │
   tophat.streaming (SignalR)   tophat.broker.projectx (REST orders)
   market+user hubs (live)      market entry + OCO brackets
```

Recommended stack: **FastAPI** backend (reuses the existing `tophat/` package as-is), **SQLite→Postgres**
for state, a lightweight **React/Svelte** frontend, WebSocket for live push. Deploy on a small always-on
VPS in/near US-East (low latency to CME/ProjectX); process manager + auto-restart; **server clock pinned
to ET/NTP** (the whole strategy keys off 09:30/09:45 ET).

### What the dashboard shows / controls
- **Accounts table** (auto-populated from ProjectX): alias, size, **phase**, **payout cycle (1–4)**,
  equity, peak/floor, today's plan (nuke / flip / idle), open position, last order status, enabled toggle.
- **Per-account toggle** + bulk enable/disable.
- **Global params** (editable, validated): nuke target, flip target, contract sizes, brackets,
  payout-cap, stagger times, max nukes/day. Changes versioned + audit-logged.
- **Schedule view**: which account nukes today, flip stagger assignments for the week.
- **Alerts feed**: payout-ready, near-miss (`MANUAL_ADJUSTMENT_REQUIRED`), bust, API/stream errors,
  hedge-guard skips.

---

## 5. Automation: the lifecycle state machine

The engine + state store already track everything needed. Automation = a **scheduler** + **rotation
logic** wrapped around the existing `run_session`.

**Per-account state already persisted** (`store/states.py`): `phase`, `equity`, `peak_equity_eod`,
`days_traded`, `payouts_taken`, `winning_days_this_cycle`, `nuke_tries_this_cycle`,
`nuke_hit_this_cycle`, `locked_out_today`. Add for full automation: `nuke_scheduled_date`,
`flip_stagger_slot`, `last_action_date`, `payout_ready` (+ optional `disabled_reason`).

**Daily automated flow (≈09:30–10:45 ET):**
1. **Auto-discover** accounts from ProjectX; new ones land in the dashboard as `disabled` pending a
   one-time human enable (your only routine touch-point). Infer phase from balance/name on first sight.
2. **Lock drive direction** at 09:45 from the stream (and the 10:15 window if running the 2nd edge window).
3. **Assign the day's plan** per enabled account:
   - **Nuke rotation:** exactly **one** account nukes per trading day, round-robin across the funded
     fleet so each nukes ~once/week. Auto-skip if no account is in a nuke cycle today.
   - **Flips:** every other due account flips, each at its **staggered slot** (09:45/10:00/10:15/…).
4. **Place orders** through `runner` → `broker.place_bracket` (market + OCO), through the **hedge-guard** (§5.1).
5. **Track fills/PnL** live via the user hub; update state; count winning days.
6. **Payout-ready → auto-disable** the account and raise an alert; **you** withdraw + re-arm (money
   movements stay manual by policy).
7. **Retire at payout 4**; flatten + log at session end; persist state.

### 5.1 Hedge-guard validation layer (the staggered-flip concern)

**Clarification first:** Topstep's no-hedging rule is **per account**, not per fleet. Two *different*
accounts holding opposite positions is **not** hedging and is completely fine — in fact it's *ideal*
for decorrelation. So staggered flips that resolve to different directions across accounts are good,
not a problem. No need to force same-direction across accounts.

**The guard you do need is per-account:** never send an order that opposes (or pyramids) an account's
existing open position. Because each account takes one flip/day and brackets resolve same-session, the
only way this bites is a still-open position (an unresolved nuke, a re-entry, or a manual position) when
a new entry is due.

**Rule (simple + safe):** before any entry on account *A*, read *A*'s live position from the user hub.
- **Flat →** place the order.
- **Has an open position →** **skip** the new entry for *A* today and log `SKIPPED_NOT_FLAT`
  (covers both the opposing-direction hedge case and the pyramiding/size-breach case).
- Optional refinement: instead of skipping outright, **defer** *A*'s flip and retry once it goes flat
  (e.g., poll until ~11:00 ET, then give up for the day). One winning day missed is harmless; an illegal
  hedge or a DLL breach is not.

This keeps every account to **one open position at a time** and makes the per-account hedge rule
structurally impossible to violate, while staggering still delivers cross-account decorrelation.

---

## 6. Execution mechanics — already solved, just validate parity

- **Drive direction (fast):** `streaming/drive.py` consumes ProjectX SignalR market-hub quotes
  (`SubscribeContractQuotes`) in real time, records the **09:30 ET open price**, tracks the running
  close, and **locks** LONG/SHORT at **09:45 ET** by comparing close vs open. This is exactly the
  "store the 08:30 CT open, compare at 08:45 CT" logic you described — already push-based, no polling.
  *To validate:* confirm the live OR open/close (first/last streamed price in the window) matches the
  backtest's first-bar-open / last-bar-close so the live signal == the tested signal.
- **Entry + brackets (no fill-price race):** `broker/projectx/brackets.py` sends a **market entry** with
  attached **OCO bracket** where target and stop are **tick offsets** (`takeProfitBracket` /
  `stopLossBracket`). ProjectX attaches them relative to the *actual fill*, so you do **not** need to
  capture the fill price and compute prices yourself — it's handled atomically. `pts_to_ticks` does the
  NQ 0.25-tick conversion.
- **To wire up for full automation:** the SignalR **user hub** is the live source for fills, positions,
  balances, and order status (the plan notes it as a follow-up to finish). It feeds the hedge-guard,
  the winning-day counter, and the dashboard.

---

## 7. Multi-API-key support (future — noted, not yet built)

This will be shared with 1–2 friends, who have their own ProjectX API keys. No auth exists today (single
operator). Two designs were considered:

1. **API key as the login.** Paste a key → it authenticates and shows that key's accounts. Simplest;
   zero account system. Downside: no identity beyond the key, no way to see accounts spanning *multiple*
   keys in one view, and the key (a live trading credential) becomes the session token.
2. **Email/password accounts + keys in Settings.** Users log in, then add one or more ProjectX API keys
   under their profile; the dashboard shows a **consolidated** view across all of their keys. More moving
   parts (user store, password hashing, sessions) but extensible and multi-key-friendly.

**Recommendation: option 2, built incrementally.** Start with a thin user table (email + hashed password +
a list of encrypted API keys) and a session cookie; render the consolidated account view by iterating each
key's broker. Key implementation note: **accounts under different API keys are fully independent** — each
key needs its own broker instance, its own SignalR streams, and its own slice of state; the dashboard just
unions them for display and scopes every action (toggle, run, settings) to the owning key. Encrypt stored
keys at rest, never return them to the client, and keep the per-key broker/stream pool keyed by user.
Until then, the single-operator mock/live broker in `tophat/server/service.py` is the path.

## 8. Phased roadmap

1. ✅ **Reconcile engine** to the locked spec (§2).
2. ✅ **Automation modules** — decorrelation scheduler (`services/scheduler.py`), hedge-guard
   (`services/guards.py`), editable settings store (`store/config.py`).
3. ✅ **Backend API + web dashboard** — FastAPI (`server/app.py`, `server/service.py`) over REST +
   WebSocket, mock broker for credential-free dev (`broker/mock.py`), modern Dashboard + Settings UI
   (`server/static/index.html`). Launch: `python tophat.py` (or `python -m tophat.server`).
4. ✅ **Cleanup pass** (§3) — archived the globex exploration, git-ignored dev data + runtime state.
5. ✅ **Live outcome → state loop** — `services/lifecycle.py` reconciles each closed trade from the
   account balance and advances the payout cycle (nuke → 4 flips → payout_ready → operator marks
   withdrawn → next cycle). Pending trades persisted on `AccountState`.
6. ✅ **Auto-fire scheduler** — `services/automation.py` background loop fires due accounts at their
   stagger times on trading days, gated by `auto_execute`; idempotent (reconcile + once-per-day).
7. ✅ **Login auth** — `server/auth.py` (SQLite users, pbkdf2 hashing, signed session cookies); all
   routes gated, `/login` page, sign-out. Create users: `python -m tophat.server.auth adduser …`.
8. ✅ **Test suite** — `tests/` (46 tests: engine, lifecycle, scheduler, auth, API, e2e) fully
   isolated via `TOPHAT_DATA_DIR` (a temp dir; never touches real `data/`). Run: `python -m pytest tests/`.
   Covers mid-day losses (flip/nuke loss, recovery), eval pass → `passed`, account `blown`, the full
   funded-$0 lifecycle, one-fire-per-day, and the hedge guard.
9. **Confirm Topstep payout rules** ([STRATEGY.md](STRATEGY.md) §6) and bake exact figures into Settings.
9. **(Optional) Real-time fills via SignalR user hub** — reconcile currently uses REST balance, which
   is sufficient for the daily cadence; the user hub would make fills push-based and intraday.
10. ✅ **Multi-user / multi-API-key** (§7, option 2 — shipped 2026-07-06): per-user data dirs
   (`data/users/<uid>/`) via `tophat/store/tenant.py`; every login user gets their own encrypted
   API keys, registry, states, settings, mirrors, sim templates, and broker pool; the automation
   loop ticks each user's fleet independently. Users are created from the server console:
   `python -m tophat.server.auth adduser friend@x.com '<password>'`. (State JSON → DB remains
   optional/not needed at this scale.)
11. **Practice-account forward test** vs backtest; then **go/no-go** and ONE live account before scaling.
