# TopHat Rabbit 🎩🐇

The copier-box bridge service — the second TopHat backend. It lives on the
Windows machine that runs Tradecopia (the same box as TopHat Watchdog), runs
24/7, and does the trick nobody watches: every morning before the session it
pulls the day's desired copier arrangement from TopHat, quietly rewrites the
Tradecopia database while the app is closed, brings the app back up, checks its
work, and reports back. If anything looks wrong it restores the backup and
tells on itself.

Design + hard rules: `docs/TRADECOPIA_AUTOMATION_PLAN.md` (§7 writer, §12
bridge). Host facts: `rabbit/fixtures.md` (Stage 0 recon, 2026-07-08).

```
TopHat (Railway/Render) ◀──HTTPS pull──  rabbit.py run     (this box, 24/7)
  GET  /api/ops/tc-desired               1. pull desired state (box token)
  POST /api/ops/tc-status                2. tc_apply: quit → backup → write
  Discord ◀── apply result                    → relaunch → verify / rollback
                                         3. push status back
```

## Files

| File | What |
|---|---|
| `rabbit.py` | the service: schedule + flag poll, HTTP, modes (`run`/`once`/`plan`/`observe`/`reconnect`/`rollback`/`status`) |
| `tc_apply.py` | the DB engine: §7 writer (guards, diff, one-transaction apply, backup, verify, rollback) + §13 read-only observer |
| `rabbit_config.json` | box config — copy from `.example`, never commit (token inside) |
| `rabbit_state.json` / `rabbit.log` / `backups/` | runtime state, log, 30-day DB backups |
| `fixtures.md` | Stage 0 recon record — the facts the writer trusts |

## Setup (per box, once)

1. Python 3.11+ on the box, then `pip install tzdata` (Windows needs it for
   the ET apply window; everything else is stdlib).
2. In TopHat: **Settings → Copier boxes → Add box** — name it, copy the token
   (shown once).
3. `copy rabbit_config.json.example rabbit_config.json` and fill in the token,
   the TopHat URL, and check the paths/guards against `fixtures.md`.
4. Dry-run: `python rabbit.py plan` — prints the diff it WOULD apply, touches
   nothing.
5. Supervised first run (Gate A, operator watching, throwaway accounts):
   `python rabbit.py once --force-window`.
6. Service: Task Scheduler → new task, run at logon/startup:
   `python C:\...\rabbit\rabbit.py run` (restart on failure). The loop
   survives crashes of individual cycles on its own.

## Behavior (cadence redesigned 2026-07-08)

- **One scheduled apply per day** at `apply_at` ET (default **22:00** — after
  evening activations/purchases). If TopHat isn't ready (409: plan not
  applied, missing account number, …) it retries every `retry_every_min`
  (15) up to `max_retries` (8), then waits for the next trigger.
- **Between applies it only polls the flag**: `GET /api/ops/tc-poll`, a
  ~30-byte "should I sync now?" check every `poll_interval_s` (60 s). That
  poll is what makes two things land within a minute: the **Sync now**
  button in Settings → Copier boxes, and the flag TopHat sets automatically
  when you click **Mark applied** (so the morning rotation applies itself
  right after you confirm the plan).
- **Never writes during market hours**: the blackout (09:30–16:10 ET,
  config) refuses every write trigger; the DB positions table is a second
  flatness guard on top.
- Every apply: full backup of db+wal+shm first (30 days kept), one
  transaction, verify after boot, automatic restore on any failure. Result
  posted to TopHat → your Discord webhook (Settings → Notifications).
- **Reverse sync rides every cycle** (plan doc §13): after the apply (even a
  no-op) it SELECTs follower balances/names out of the DB and POSTs them to
  TopHat, which books fresh balances into mirror bookkeeping (anchor-aware),
  flags stale ones, and proposes unknown accounts for one-click onboarding.
  Run it alone any time with `python rabbit.py observe` — read-only, safe
  while the app is up.
- `python rabbit.py reconnect` = quit + relaunch with **no DB write** — the
  standard fix for a dead Topstep session at any flat moment (§11.3).

## What it will never do

Onboard new broker accounts, touch `accounts`/`entities`/token material,
insert `feeds` rows, force-kill the app, write while the app is running, or
apply anything when the schema/user/name/position guards disagree — those all
abort loudly instead (see §10 hard rules).
