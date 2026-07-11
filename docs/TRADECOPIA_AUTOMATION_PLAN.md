# Tradecopia DB Automation — Implementation Plan

**Status: STAGES 0–1 BUILT (2026-07-08). Written 2026-07-06.**

> ## Status log — 2026-07-08
>
> The bridge service is named **TopHat Rabbit** (`rabbit/` in this repo;
> operator-chosen, supersedes "copier-sync"/"tc-agent" everywhere below).
>
> - **Stage 0 complete** → `rabbit/fixtures.md`. Corrections to §3 landed
>   there (association pks are app-assigned accounts.id on AUTOINCREMENT
>   columns; `replication_disable_reason` column exists; leader rows carry
>   entity_id+account_name; live groups flags are (0,1,1,1) not all-1s).
>   The app log is encrypted → verify waits `boot_wait_s` instead of log
>   markers. **Operator sign-off of fixtures.md still pending.**
> - **Stage 1 complete**: writer engine `rabbit/tc_apply.py` (+14 tests in
>   `tests/test_tc_apply.py`: golden diffs, guards, idempotency, mid-txn
>   crash, verify-fail rollback), exporter `tophat/services/tc_export.py`
>   (+tests), box-token pairing `tophat/store/boxes.py` + Settings → "Copier
>   boxes" UI, endpoints `GET /api/ops/tc-desired` / `POST /api/ops/tc-status`
>   (bearer-token authed, tenant-bound, Discord notify on non-noop results),
>   and the service loop `rabbit/rabbit.py`.
> - **Cadence (operator-designed 2026-07-08, supersedes both the nightly
>   one-shot and the first 24/7 heavy-poll draft):** Rabbit runs 24/7 but
>   applies on **one scheduled pull per day** (`apply_at`, default 22:30 ET —
>   after activations/purchases are done, and clear of TopHat's 22:00 OCO
>   probe so the two never share a log minute), with **retries** every 15 min
>   (max 8) while TopHat answers 409 (plan not applied / data incomplete).
>   Exhausting the retries is LOUD (2026-07-09): Rabbit pushes a `gave-up`
>   status that TopHat fans to Discord — a silent skip would leave Tradecopia
>   running yesterday's mapping all the next day.
>   In between it polls only `GET /api/ops/tc-poll` (a ~30-byte flag) every
>   60 s, which is what makes two triggers land within a minute: the
>   Settings **"Sync now"** button, and the automatic flag TopHat sets on
>   **"Mark applied"** — that second one closes the date-rollover gap (the
>   apex nuke rotation is date-keyed, so the morning confirm re-syncs the
>   box without anyone touching it). Writes always refuse inside the
>   **market-hours blackout** (09:30–16:10 ET, config) whatever the trigger;
>   the §7.1 narrow morning window is retired in favor of blackout +
>   positions flat-guard.
> - **§5.1 contract amendment (v2):** the `guard` block (goose version +
>   Tradecopia user id) moved into the box's `rabbit_config.json` — they are
>   per-host facts a hosted TopHat cannot know. The payload carries `tenant`
>   instead (tenant-keyed from day one per §12.2).
> - **Guard addition:** preflight also aborts if `positions.net_pos != 0`
>   for any touched account (belt-and-braces flatness check).
> - **§13 reverse sync: R1 BUILT (2026-07-08)** — reader
>   `tc_apply.read_observed` (SELECT-only, rides every cycle + standalone
>   `rabbit.py observe`), importer `tophat/services/tc_observe.py`
>   (freshness gate 36 h, `MirrorAccount.start_balance` anchor with
>   fresh-phase auto-capture, onboarding proposals never auto-created),
>   endpoints `POST /api/ops/tc-observed` + `GET /api/ops/tc-observed/last`,
>   summary line folded into the Rabbit status (§13.8) and shown in
>   Settings → Copier boxes. Deviations from the sketch, on purpose:
>   observations live in the per-tenant snapshot file, not as new mirror
>   fields (`last_day_pnl` feeds the solver and must not have two writers);
>   the reader payload adds `entity_organization` for firm inference.
>   **R2 (supervised live pull) pending** — needs the live follower fleet
>   connected in Tradecopia.
> - **Idle guards (operator 2026-07-08):** three independent layers keep a
>   leader-only (no-copy-trading) stretch silent. (1) No box paired → no
>   token exists → nothing anywhere makes a call (pull-only architecture).
>   (2) Exporter refuses (409 "copy trading is idle") while there are no
>   enabled, non-terminal mirrors — a running Rabbit never churns the app
>   to apply an empty world. Mirrors that exist but are deliberately
>   unmapped still export `groups: []` (unmap-all stays possible).
>   (3) Settings master switch `copier_sync_enabled=false` → every pull
>   gets 409 "disabled in Settings", boxes stay paired but inert.
> - **Next: Stage 2 (Gate A)** — supervised run on throwaway accounts, §9.2
>   checklist + the §11.4 auto-login test, operator present. Then Stage 3
>   supervised dailies. Reverse-sync R2 can ride the same session.
**Goal:** eliminate the ~10-min/day manual Tradecopia edit session. TopHat already
computes the daily copier plan (docs/MULTI_FIRM_PLAN.md §5); this plan makes a
machine apply it to Tradecopia by writing its SQLite DB while the app is closed,
then relaunching it — the app fully rebuilds copier state from the DB at boot.

This document is a **handoff spec**: it is written so that implementation agents
can execute it stage by stage without any other context. Read the whole document
before writing any code. Every stage has an exit gate; **do not start a stage
until the previous stage's gate has passed and, where marked, the operator has
signed off.**

---

## 0. Verdict and evidence (why this approach)

A reverse-engineering pass on the Tradecopia desktop binary (2026-07-06,
separate session) established:

1. **No file watching.** No fsnotify/directory-watch in the binary (the only
   "watcher" symbols belong to its embedded NATS messaging library). An external
   write to `tradecopia-desktop.db` while the app runs is **not** noticed.
2. **The copy engine is stateful in memory.** `ReplicationManager` / `FeedManager`
   hold live state; UI edits mutate memory and DB together
   (`ToggleFollowerReplication`, `updateAllFollowerReplication`). External DB
   inserts update the file, not the running engine.
3. **More state than the association tables.** Every account in a group also has
   a `feeds` row (confirmed for all 7 live accounts) plus a live broker connection.
4. **Startup reconstructs everything from the DB.** Boot-time log strings:
   `All entities restored successfully`, `CreateFeedsForAllGroups`,
   `Group is active`, `refreshing feeds for updated group`, and
   `Auto-deleting orphaned group without leader account`.

Therefore the working pattern is **quit app → write DB → relaunch**, run once
per day pre-session. Verdict: **viable**. The boot-time "orphan group
auto-delete" is actually an asset — it means the app *reconciles* whatever it
finds at boot, and a malformed write surfaces as deterministically missing rows
we can detect and roll back (see §8).

**Caveats that shape this plan:**
- The feeds-rebuild-at-boot behavior is inferred from static analysis. It is
  **Gate A** (§9.2): proven empirically on throwaway accounts before any code
  touches an account that matters.
- "Boot restores all entities" is **only true while stored broker tokens are
  fresh** — see §11 (2026-07-07 evidence). A relaunch reliably restores the
  ProjectX/Topstep connection (24 h token re-minted from the stored API key)
  but does **not** restore a Tradovate entity whose 80-minute token has gone
  stale; that needs a manual UI re-login (or the §11.4 auto-login hypothesis
  to prove out at Gate A).
- All table/column facts below were read from one machine's DB at migration
  version `20260521000000`. They are **assumptions to re-verify in Stage 0**,
  not eternal truths. The writer must re-check them at every run (schema guard).
- Tradecopia auto-updates could change the schema at any time. The schema guard
  turns that into a clean abort instead of a silent bad write.

---

## 1. Scope

### In scope
- Rearranging copier roles among accounts **already connected** in Tradecopia:
  which account leads, who follows whom, per-follower scale, replicate on/off,
  group membership.
- One writer run per day, pre-session, after TopHat's copier plan is final.
- Automatic backup, post-relaunch verification, automatic rollback on failure.

### Out of scope — the writer must refuse, not attempt
- **Onboarding new broker accounts.** New accounts need auth tokens the app
  obtains through its own broker login (encrypted tokens in `entities` /
  `*_key` files). The writer never INSERTs into `accounts` or `entities` and
  never reads or writes token/key material.
- Writing the DB while the app is running.
- Placing, modifying, or closing any order/position.
- Editing anything for a `user_id` other than the operator's.

---

## 2. System context

```
┌─────────── TopHat (this repo) ───────────┐      ┌────── Tradecopia host ──────┐
│ nightly solver (services/copier_plan.py) │      │  tc-apply (new, standalone) │
│   → copier plan (desired mappings)       │      │    1. quit Tradecopia       │
│   → NEW: exporter writes                 │─────▶│    2. backup DB (3 files)   │
│     tc_desired_state.json                │      │    3. write rows (1 txn)    │
│ Operations page: "Mark applied" flips    │◀─────│    4. relaunch Tradecopia   │
│   mirrors to the new mapping (existing)  │status│    5. verify → ok / rollback│
└──────────────────────────────────────────┘      └─────────────────────────────┘
```

- **Leaders** = Topstep/ProjectX accounts (TopHat trades them via API), including
  the practice signal accounts (PRAC-*) that host the Apex-rule signal channels.
- **Followers** = Tradovate-based firm accounts (Tradeify, Lucid, Apex) that only
  Tradecopia can trade.
- The two halves talk through **two files** (contract in §5): a desired-state
  JSON (TopHat → tc-apply) and a run-status JSON (tc-apply → TopHat). No RPC, no
  shared code; either side can be tested alone.

---

## 3. Tradecopia DB contract (as observed at goose version 20260521000000)

File: `tradecopia-desktop.db` (SQLite, WAL mode: `-wal` and `-shm` siblings).

| Table | Role | Key facts |
|---|---|---|
| `goose_db_version` | migration version | current = `20260521000000`; **abort if ≠ expected** |
| `accounts` | every connected broker account | source of `id` and `name`; **read-only** |
| `entities` | broker connections + encrypted auth | has `user_id`; **read-only, never touch** |
| `groups` | one copy-group per leader | `id` = UUID string, `user_id`, `status='active'`, flags `prevent_hedging`, `auto_close_follower_positions`, `position_reconciler_enabled`, `disable_replication_on_reconcile` all default **1** |
| leader association table (expected `group_leader_accounts`; **confirm exact name in Stage 0**) | leader row per group | row **primary key = the leader's `accounts.id`**; a group without a leader row is auto-deleted at boot |
| `group_follower_accounts` | follower rows | pk = follower's `accounts.id`; `entity_id`, `account_name`, `scale` (REAL, e.g. `1.0`), `contract_type` (`'Standard'` in live data), `replicate` (1/0), group reference |
| `feeds` | per-account live feed wiring | **never write** — delete rows for touched accounts and let boot recreate them (Gate A validates) |

Invariants (all verified against the live DB on 2026-07-06):

- Single user: `user_id = 9931744e-779b-4bdf-8782-1a0eebedc2ac` on every
  `groups` and `entities` row. The writer takes this as config, re-verifies it
  is the **only** user id present, and stamps it on every row it writes.
- Association-row pk = the account's `accounts.id`, for the leader and all six
  followers. Every row the writer produces is derivable from `accounts`.
- No triggers exist. **Referential integrity is entirely on the writer**:
  `entity_id` must exist in `entities`, account ids/names must exist in `accounts`.
- New `groups.id` = freshly generated UUID (lowercase, hyphenated, v4).

> Stage 0 dumps the real schema and records the exact column lists. Anything in
> this section that disagrees with the dump is resolved **in favor of the dump**,
> with the correction recorded in `fixtures.md` and operator sign-off.

---

## 4. Host facts to pin in Stage 0 (fixtures)

Record in `fixtures.md` next to the tool (values below are unknowns, not defaults):

- [ ] Absolute path of `tradecopia-desktop.db` (+ confirm `-wal`/`-shm` siblings).
- [ ] Full output of `.schema` for the 6 tables in §3 (+ `PRAGMA journal_mode`).
- [ ] Exact leader-association table name and column list.
- [ ] Whether `group_follower_accounts` carries the group reference as
  `group_id` (assumed) — and its exact column list.
- [ ] Process name of the running app (e.g. `TradeCopia.exe` — verify) and
  whether it spawns child processes.
- [ ] Path of the launch executable / shortcut, and any required launch args.
- [ ] App log file location (for the boot-complete markers in §0.4), if readable.
- [ ] Typical boot duration to `All entities restored successfully`.
- [ ] The 7 live account names ↔ `accounts.id` map; which are leaders today.
- [ ] Confirm Topstep account names in Tradecopia `accounts.name` are **identical**
  to the ProjectX names TopHat sees (e.g. `50KTC-V2-…`, `PRAC-V2-…`). If they
  differ, record the mapping rule — the exporter (§6) depends on it.

---

## 5. File contracts between TopHat and tc-apply

### 5.1 `tc_desired_state.json` (TopHat → tc-apply)

Declarative end-state (never a diff). Keyed by **account name**, never by
Tradecopia-internal id:

```json
{
  "version": 1,
  "generated_at": "2026-07-07T08:05:00-04:00",
  "plan_date": "2026-07-07",
  "guard": {
    "goose_version": 20260521000000,
    "user_id": "9931744e-779b-4bdf-8782-1a0eebedc2ac"
  },
  "groups": [
    {
      "leader": "PRAC-V2-9790-61284984",
      "followers": [
        {"account": "APEX-282-xxxx", "scale": 1.0, "replicate": true,
         "contract_type": "Standard"}
      ]
    }
  ]
}
```

Semantics:
- Every account name (leader and follower) **must** resolve to exactly one
  `accounts.name`. Zero or multiple matches → abort, no write.
- An account absent from the file but present in a Tradecopia group is
  **removed from its group** (drop its follower row; if a leader whose group
  is gone, drop the group + leader row). Absence = unmapped, by design — the
  file is the complete desired world.
- `replicate: false` keeps the mapping but pauses copying (mirrors the UI toggle).

### 5.2 `tc_apply_status.json` (tc-apply → TopHat)

```json
{
  "plan_date": "2026-07-07",
  "started_at": "…", "finished_at": "…",
  "result": "applied | noop | aborted | rolled_back",
  "detail": "human-readable one-liner",
  "changes": {"groups_created": 1, "groups_dropped": 0, "followers_upserted": 3,
               "followers_dropped": 1, "feeds_cleared": 4},
  "verify": {"groups_ok": true, "feeds_rebuilt": true}
}
```

`applied` and `noop` are the only results TopHat may treat as success.

---

## 6. TopHat-side work (this repo) — exporter

Small and self-contained; touches no trading paths.

1. **Exporter** in `tophat/services/` translating the day's applied desired
   mapping into `tc_desired_state.json`:
   - Source of truth: the mirrors store (`tophat/store/mirrors.py`) — each
     enabled, non-terminal `MirrorAccount` with `leader_id != None` becomes a
     follower entry under its leader; `multiplier` → `scale`;
     `account_number` → `account`.
   - Leader names: resolve `leader_id` → broker account name via the cached
     snapshots (`service.account_names`), same as the Operations page does.
   - Refuse to export while today's plan has unapplied lines (the plan must be
     marked applied first — the mirrors store is only current after that), and
     when any needed `account_number` is empty. Surface both as errors on the
     Operations page.
2. **Endpoint** `GET /api/ops/tc-desired` returning the JSON (operator downloads
   or a scheduled task fetches it), plus a copy written next to the day's plan in
   `data/users/<uid>/copier_plans/`.
3. **Tests** mirroring `tests/test_copier_plan.py` idioms: mirrors fixture in →
   exact JSON out; unmapped/disabled/terminal mirrors excluded; empty
   `account_number` → error.

Explicitly not in v1: consuming `tc_apply_status.json` to auto-flip "Mark
applied" (Stage 4 option, §9.4).

---

## 7. tc-apply — the writer (new standalone tool, Tradecopia host)

Single CLI, stdlib-only (Python 3.11+: `sqlite3`, `argparse`, `json`, `shutil`,
`subprocess`, `uuid`). No third-party deps, no daemon. Modes:

```
tc-apply plan   --desired tc_desired_state.json          # print diff, touch nothing
tc-apply run    --desired tc_desired_state.json          # full stop→write→start→verify
tc-apply verify                                          # post-boot checks only
tc-apply rollback --backup <dir>                         # manual restore
```

### 7.1 Run sequence (`run`)

1. **Preflight (read-only, app may be running):**
   - Load desired state; validate JSON shape and `version`.
   - Open DB **read-only**; check `goose_db_version` == guard value; check the
     single `user_id` == guard value; resolve every account name; build the diff.
   - Diff empty → write status `noop`, exit 0 (app untouched).
   - Time-window guard: refuse outside the configured window (default
     **06:00–09:15 ET**) unless `--force-window`. Followers must be flat; the
     pre-session window is how we guarantee it (Tradecopia state can't tell us).
2. **Stop the app:** graceful close (`taskkill /IM <exe> /T` *without* `/F`,
   or WM_CLOSE); poll until the process tree is gone **and** the DB accepts an
   exclusive lock (`BEGIN IMMEDIATE` succeeds). Timeout (default 60s) → abort,
   status `aborted`, **never** force-kill in v1 (a mid-write kill of a trading
   app is worse than a skipped day; the operator gets an alert instead).
   Record whether the app was running (relaunch decision).
3. **Backup:** copy `tradecopia-desktop.db`, `-wal`, `-shm` (those that exist)
   to `backups/<plan_date>_<hhmmss>/`. Keep ≥ 30 days. Backup failure → abort
   before any write.
4. **Write — one transaction** (`BEGIN IMMEDIATE` … `COMMIT`), minimal diff,
   deterministic order:
   - For each desired group: if the leader already leads a group → **reuse that
     `groups.id`** (stable identity, minimal feed churn); else INSERT `groups`
     (fresh UUID, guard `user_id`, `status='active'`, all four flags = 1) and
     the leader association row (pk = leader `accounts.id`).
   - Upsert follower rows (pk = follower `accounts.id`): group ref, `entity_id`
     (from the account's `entities` link), `account_name`, `scale`,
     `contract_type`, `replicate`.
   - Delete follower rows not in the desired state; delete undesired groups
     (group + leader row + remaining follower rows).
   - **Never leave a group without its leader row** — orphan groups are
     auto-deleted at boot and would silently drop followers.
   - Delete `feeds` rows for every account whose role/group/scale/replicate
     changed (and for every account of dropped groups). Never insert feeds.
   - Any SQL error → ROLLBACK, restore backup anyway (belt and braces),
     status `aborted`.
5. **Relaunch** the app (if it was running / `--start-always`).
6. **Verify (read-only), after boot settles** (log marker
   `All entities restored successfully` when readable, else fixtures boot time
   + margin):
   - Every desired group/leader/follower row still present (nothing
     auto-deleted at boot ⇒ the app accepted the write).
   - A `feeds` row exists per managed account (proves the rebuild).
   - Pass → status `applied`. Fail → **quit app, restore the three files from
     backup, relaunch, status `rolled_back`,** alert loudly.
7. Always write `tc_apply_status.json` last, whatever happened.

### 7.2 Failure matrix

| Failure | Response |
|---|---|
| Schema/user guard mismatch | abort before stopping the app; alert "Tradecopia updated — re-run Stage 0 recon" |
| Unresolvable account name | abort before stopping the app (likely a new not-yet-onboarded account) |
| App won't quit in time | abort; leave everything as-is; alert |
| SQL error mid-transaction | rollback txn + restore backup; relaunch; alert |
| Verify fails after boot | restore backup; relaunch; alert |
| Restore itself fails | **stop; no retries**; leave backup dir path in status + alert — operator restores by hand |
| Two writers / re-entry | lock file beside the DB; second instance exits `aborted` immediately |

### 7.3 Writer test suite (no app, no live DB)

- Fixture DB built from the Stage-0 schema dump with synthetic accounts.
- Golden tests: desired state in → exact row set out (create, move, scale
  change, replicate toggle, unmap, no-op idempotency — running twice changes
  nothing, group UUIDs stable across runs).
- Guard tests: wrong goose version, wrong/extra user_id, unknown account name,
  duplicate names → abort with no write.
- Crash-safety test: kill the writer mid-transaction (fault injection) → DB
  intact (WAL rollback) or restorable from backup.

---

## 8. Why verification works (the canary)

The boot sequence *is* the acceptance test: Tradecopia reconciles the DB at
launch and deletes what it considers invalid (`Auto-deleting orphaned group…`).
So "our rows survived boot + feeds exist" is strong evidence the engine wired
the arrangement. The one thing static analysis can't prove — that a rebuilt
feed actually **copies a live trade** — is exactly what Gate A proves once,
with a human watching, before anything is automated.

---

## 9. Stages and gates

### Stage 0 — Recon (read-only; agent + operator ~30 min)
Fill every fixture in §4 from the live host (SQLite CLI, read-only). No writes.
**Gate:** `fixtures.md` complete; §3 corrections (if any) recorded; operator
sign-off.

### Stage 1 — Writer built against fixtures (agent, no live anything)
Implement §7 + §7.3 tests + §6 exporter (independent; can be parallel).
**Gate:** full test suite green; `tc-apply plan` output for a hand-written
desired state reviewed by the operator and judged correct.

### Stage 2 — Gate A: one supervised empirical run (operator present)
On demo/practice/throwaway accounts only:
1. Quit Tradecopia; confirm the process tree is gone.
2. `tc-apply run` with a desired state that moves **one** follower to a
   different leader's group and flips one `replicate`.
3. Confirm in the UI: new arrangement shown; feeds row recreated & connected.
4. Fire a test trade on the leader (practice signal account) → copies to the
   new follower set.
5. Negative test: edit the DB while the app runs → confirm it's ignored until
   restart (documents the boundary).
**Gate:** all five observed by the operator. **Any surprise → stop, update this
doc, redo from the stage that assumption came from.**

### Stage 3 — Supervised dailies (operator eyeballs, ~1 week)
Scheduled pre-session run (Windows Task Scheduler, inside the §7.1 window):
export from TopHat → `tc-apply run` → operator glances at the Tradecopia UI
before the session. Keep the manual "Mark applied" click in TopHat.
**Gate:** 3 consecutive clean `applied`/`noop` days, zero manual corrections.

### Stage 4 — Unattended
Alerting on any non-`applied`/`noop` status (operator's choice of channel).
Optional: TopHat consumes `tc_apply_status.json` and flips "Mark applied"
automatically. Fallback is always graceful: any failure leaves either the old
arrangement or a restored backup, and the operator can do the 10-minute manual
edit that day.

---

## 10. Hard rules for every implementing agent

1. **Never** write the DB while the Tradecopia process exists.
2. **Never** touch `accounts`, `entities`, tokens, or `*_key` files.
3. **Never** insert into `feeds` — delete-and-let-boot-rebuild only.
4. **Never** force-kill the app in v1.
5. **Never** write without a same-run backup of all three DB files.
6. Guard mismatch (schema version, user id, name resolution) = abort, not adapt.
7. All writes in one transaction; partial application must be impossible.
8. Desired state is declarative and complete; the writer computes the diff —
   TopHat never sends imperative edit commands.
9. When observed reality contradicts this document, **stop and update the
   document first** (with operator sign-off), then code.

---

## 11. Broker-connection lifecycle (evidence 2026-07-06/07) and operating rules

Read directly from the live DB (`entities`, `notifications`) and TopHat's logs.
All times ET unless marked.

### 11.1 What actually keeps each connection alive

| Side | Stored credential | Session token | Runtime renewal | After a socket break | After an app relaunch |
|---|---|---|---|---|---|
| **Tradovate** (Apex now; Lucid/Tradeify followers later) | encrypted token blob only (`entities.auth_token`, 428 chars) | **80 minutes** (`auth_token_expiry` = created + 1:20 exactly) | yes, in-memory while healthy (ran 6.5 h on one login); the DB row is never refreshed | reconnect FAILS once the stored token is stale: `broker:connection:auth:expired` nag **every 2 min, forever**; no auto-relogin | **stale token ⇒ not restored** (observed: 2026-07-06 20:34 CT boot came up Disconnected); vendor help (operator-read 2026-07-07) says closed **> 60–90 min ⇒ manual reconnect via Connections tab** — which conversely means a brief relaunch of a *healthy* session (tokens are being refreshed continuously) stays inside the window. The nightly quit→write→relaunch relies on exactly this; verify at Gate A |
| **ProjectX/Topstep** | encrypted API key (96 chars — key, not a session) | **24 h**, minted at boot via loginKey | none needed within the day | reconnects fine while the 24 h token is valid (survived the 07:52 ET blip) | **restored reliably** (fresh loginKey every boot) |

### 11.2 The two 2026-07-07 drops, reconstructed

- **07:52:49 ET** — both entities' sockets died in the same second while TopHat's
  HTTP polling (same machine) kept returning 200s at ~50 ms: a local socket-level
  event (NIC/Wi-Fi/power), not an internet outage. Topstep reconnected in 9 s
  (token valid). The Tradovate side entered the terminal auth-expired loop
  (token stale since ~02:39 ET) — dead until manual re-login.
- **09:14:47 ET** — Topstep session killed **server-side**: Tradecopia retried
  for 3 m 13 s (SignalR 1 m/5 s pattern), permanently gave up 09:18:00 ET.
  **Not** TopHat's REST traffic: TopHat's own session (same ProjectX user)
  stayed valid all day (zero 401s), its polling coexisted with Tradecopia's
  session for 7 h, and its fresh loginKey at 07:53 ET did not kick anything
  for 81 minutes. **Resolved 2026-07-07 evening: the operator confirms a
  TopstepX web-dashboard login that morning** — user-hub contention from the
  dashboard is the working explanation (a ProjectX session sweep remains the
  fallback theory). A similar kick on 2026-07-06 hit at 21:41 ET, 6 min after
  a Tradecopia boot.

### 11.3 Operating rules derived

1. **TopHat's REST order placement cannot drop the copier** — no shared socket,
   demonstrated coexistence. The real failure mode is a connection that is
   *already* dead when the trade fires. Guard = watchdog, not fire-time logic.
2. **Do not attach anything else to the copier's ProjectX user during market
   hours**: no TopstepX web dashboard, and no TopHat TUI — `tophat/ui/app.py`'s
   StreamManager opens `/hubs/user` on the same user and is exactly the
   contention class suspected in 11.2. The headless server (REST-only) is safe.
3. The **Watchdog** (`deploy/watchdog.py`, live since 2026-07-07) runs one-shot
   at **09:00 and 09:25 ET** (the 09:14 kick beat a 09:00-only check) posting
   only when action is needed, plus an 11:00 ET recap that always posts
   (drive, per-account outcomes, payout-ready, drops) — the daily recap doubles
   as the heartbeat proving the scheduler itself is alive.
4. tc-apply gains a **`reconnect` mode**: quit → relaunch, **no DB write** —
   the standard fix for a dead Topstep connection at any flat moment.
   (Fixes ProjectX deterministically; fixes Tradovate only if §11.4 lands.)

### 11.4 Open item — Tradovate auto-relogin hypothesis

`%APPDATA%/tradecopia/auto_login_credentials` appeared 2026-07-07 00:21 CT,
the minute the operator re-logged the Apex entity with "remember me"-style
options. If the app now re-logins Tradovate entities at boot from stored
credentials, a relaunch heals everything and §11.1's worst row improves to
"restored". **Test at Gate A** (kill the Apex session, relaunch, observe).
Until proven, plan for: Topstep = self-healing daily; Tradovate = manual
re-login when (not if) it drops, surfaced by the watchdog.

---

## 12. Multi-user topology (TopHat hosted; 5–10 operators) — design sketch

Context: TopHat is per-user multi-tenant already (`store/tenant.py`,
`data/users/<uid>/`). The hosted deployment (Railway/Render) cannot reach into
anyone's home network, and Tradecopia only exists as a Windows desktop app, so
**every deployment shape needs a small agent on the Tradecopia host**. That
agent is tc-apply grown one step: **Copier Sync** (`copier-sync`; the name says
what it does — it syncs the copier to the night's plan. Formerly "tc-agent").

**Decision 2026-07-07: per-user Tradecopia instances (shape B below).**

### 12.1 The bridge: Copier Sync pulls, TopHat never pushes

```
┌────────── TopHat (Railway/Render) ─────────┐        ┌──── Tradecopia host (Windows) ────┐
│ per-tenant mirrors/copier plan (existing)  │        │ copier-sync (nightly, ~03:00 ET): │
│ GET  /api/ops/tc-desired   (box token) ────┼──HTTPS─▶ 1. pull desired state            │
│ POST /api/ops/tc-status    (box token) ◀───┼────────┤ 2. tc-apply run (§7, unchanged)   │
│ Discord webhook: apply result + watchdog ──┼─▶      │ 3. push status back               │
└────────────────────────────────────────────┘        └───────────────────────────────────┘
```

- **Headless by design**: copier-sync is a background script run by Windows
  Task Scheduler — no UI of its own. Its "UI" is TopHat: the Operations page
  shows the last pulled plan and the last apply result; Discord gets the
  nightly status line. Debugging happens via its local log file on the box.
- **The box dials out** (pull): no inbound port on a residential box, no
  dynamic DNS, no port forwarding — outbound HTTPS to the Railway URL is all
  it ever opens, which works behind any home NAT. Nightly cadence — latency
  is irrelevant by design.
- **Auth / pairing**: TopHat Settings gains a **"Copier boxes"** section — the
  operator names a box, TopHat mints it a long-random bearer token (scoped to
  exactly the two `/api/ops/tc-*` endpoints, bound server-side to that tenant),
  and the token goes into the box's local `copier_sync_config.json`. The same
  section shows last check-in time and last apply result per box; revoke =
  delete the token row. This is the field that links a user to their box.
- The §5 file contracts stay the wire format (the JSON bodies are the files);
  a host can still run air-gapped by downloading the file by hand.
- **App relaunch is fully automated** already by §7.1's run sequence (graceful
  quit → exclusive-lock check → write → relaunch → verify → rollback on
  failure) — no human in the loop on a normal night. The §11 nuance is what
  the relaunch *restores*: Topstep always (API-key relogin at boot); Tradovate
  provided the app was healthy at quit and downtime stays well under an hour.

### 12.2 One shared Tradecopia instance vs one per user

| | **A. Shared instance** (one Windows host, all users' followers in one app) | **B. Instance per user** (each user: own host + app + copier-sync) |
|---|---|---|
| Cost | one machine (~$30–60/mo VPS or a home box) split N ways | ~$25–45/mo Windows VPS **per user** |
| Contract change | **required**: desired-state keyed by tenant; "absence = removed" must become **per-tenant** (an agent may only delete rows for accounts its tenants own — a global-union writer would delete everyone else) plus a registry guaranteeing an account number maps to exactly one tenant | none — today's single-user contract as-is |
| Blast radius | one bad write / one app crash / one broker ban question affects everyone | contained per user |
| Broker logins | all users' Tradovate/ProjectX credentials typed into ONE app owned by whoever runs the box (trust + ToS question — resolve before building) | each user keeps their own |
| Tradovate §11 manual re-logins | the shared-box operator does everyone's | each user does their own |
| Verdict | cheapest, but couples uptime, trust, and liability | **start here**; A stays possible later because the tenant-keyed export (below) is a superset |

Decision (design-ahead, build later): implement the exporter **tenant-keyed
from day one** — `tc_desired_state.json` gains `"tenant": "<uid>"` and the
writer refuses to touch accounts outside its configured tenant set. Shape B is
then just "every copier-sync configured with one tenant", and shape A becomes
one configured with several — no rewrite either way.

### 12.3 Not building yet (explicitly)

Multi-user is **not started**. Prerequisites before any code: Stage 0–2 of the
single-user plan complete and stable (§9), the §11.4 Gate-A answer, and the
shared-instance trust/ToS question answered by the operator. The only thing to
do *now* is what §12.2 already fixed: keep every new contract field
tenant-aware so nothing has to be redesigned.

### 12.35 Operations page rework — NEXT STEP after the integration lands

Operator decision 2026-07-08, explicitly **not** this build's priority: once
Rabbit is applying plans (Stage 3+), the Operations tab stops being a to-do
list of edits the operator must make in Tradecopia and becomes an
**informational overview of what will happen / just happened automatically**:

- tonight's desired arrangement (the last `tc_desired` export) instead of
  imperative "MAP/UNMAP" instructions;
- the last apply result per box (`tc_apply_status` + Copier-boxes check-ins)
  — applied/noop/aborted/rolled_back, row counts, timestamps;
- keep the payout queue / buy list / hazards (still human actions);
- "Mark applied" likely becomes automatic on an `applied` status (the §9.4
  Stage-4 option) — decide when reworking.

### 12.4 Discord reporting (live today, grows with the stages)

- **Now**: the Watchdog posts a pre-market warning (09:00 + 09:25 ET checks,
  silent when all clear) and an always-on 11:00 ET recap (drive, per-account
  outcomes, payout-ready, drops-today) — the recap is the daily heartbeat.
- **Stage 3+**: tc-apply posts its §5.2 status (applied/noop/aborted/
  rolled_back + row counts) to the same webhook after every nightly run.
- **Stage 4**: any non-applied/noop result and any intraday
  `entities.is_connected` flip during 09:00–16:00 ET page the operator.

---

## 13. Reverse sync — observed state (Tradecopia DB → TopHat)

**Status: PLANNED (no code yet). Written 2026-07-08.**

Everything above is one-directional: TopHat computes the copier plan, tc-apply
writes it into Tradecopia. This section adds the **other direction** — a
**read-only** pull of live account facts *out* of the same DB and back into
TopHat. It exists because **there is no API for the follower firms (Lucid, Apex,
Tradeify)**; their accounts live only inside a broker platform that Tradecopia
connects to, and Tradecopia persists what it polls. Its SQLite file is therefore
the only machine-readable source of a follower's **balance** and **name** on the
whole host. The reverse sync turns that file into TopHat's follower-side
equivalent of the API-fed leader snapshot.

### 13.0 Verdict and evidence (why this is worth doing)

A read of the live DB on 2026-07-08 (read-only copy, app untouched) established
that `accounts` is a full per-account state row, not just copier wiring:

| Column (`accounts`) | Live example | What it gives TopHat |
|---|---|---|
| `name` | `PAAPEX363570000064`, `50KTC-V2-DLL-14624-20981325` | the firm's real account id → **auto-onboarding**, no manual entry |
| `balance` | `50237.0`, `141437.93` | the account's **real balance** (absolute $) |
| `realized_pn_l` | `0.0` | booked **day** P&L |
| `week_realized_pn_l` | `0.0` | booked **week** P&L (Apex consistency window) |
| `entity_id` | `APEX_36357-demo` | which broker login / firm |
| `updated_at` | `2026-07-07 06:52` | **freshness stamp** (see 13.1) |
| `is_hidden` | `0` | operator-hidden accounts to skip |

Corroborating tables (present, deeper detail, **not** required for v1):
`cash_balances` (point-in-time snapshots incl. `amount_sod` = start-of-day
balance), `cash_balance_logs` (per-fill ledger: `delta`, `cash_change_type` ∈
{`Commission`,`TradePaired`,`NewSession`}, `trade_date`), `positions` /
`position_logs` (live net position, symbol, realized/unrealized P&L).

**This mechanism is proven, not hypothetical**: the six May-29 Apex demo
followers carry real `balance` rows (`50422.1`, `50356.4`) written by the same
`feeds.connection_type='balance_polling'` (2 s interval) that runs for *every*
account regardless of entity — leader or follower. The reverse sync is a
`SELECT`; it adds no new failure mode to the write path.

**Verdict: build it, and it can ship before the writer.** The observe path is
pure read against the same DB snapshot §7.1 already takes; it delivers value
(auto balances + auto onboarding) even if the DB-write half of this plan is never
turned on.

### 13.1 The governing caveat — freshness equals connection uptime

Tradecopia's balance is only as fresh as its last **connected** poll. The live
DB shows this in one glance — two clusters, same schema:

- Topstep accounts: `updated_at = 2026-07-07 06:52` (last connected session).
- Apex followers: `updated_at = 2026-05-29` — **six weeks stale**.

At read time **both** entities were `is_connected = 0` and every
`feeds.connection_status = 'disconnected'`. The number never goes *wrong* — it
freezes, and `updated_at` stops advancing, exactly per the §11 Tradovate
80-minute-token lifecycle. Consequences that shape the whole design:

1. This is a **"balance as of `updated_at`"** feed, never a real-time one — which
   matches the intended **once-per-day** cadence and the nightly quit→relaunch
   window when the connection is known-good.
2. `updated_at` (and the `is_connected` join) **must** travel with every value
   all the way to the TopHat UI. The importer treats stale rows as *observations
   to display*, not *balances to book* (freshness gate, 13.4).
3. The only follower balances in the DB today are the stale demo Apex rows; the
   **live follower fleet is not yet onboarded** in Tradecopia (recon: the topstep
   entity is on `rizzbizzy786` with old accounts, the live fleet is on
   `rayyyu12`). The pipe is proven; live follower data appears once those
   accounts are connected in-app.
4. Leader (Topstep) balances are **already** authoritative in TopHat via the
   ProjectX API (`service.account_names_and_balances`). The reverse sync's unique
   value is the **followers** — so it may skip leader-entity accounts entirely
   (13.5).

### 13.2 Scope

**In scope**
- Read-only extraction of `accounts` (name, balance, day/week P&L, entity,
  freshness) plus the `entities.is_connected`/`status` join, for **follower**
  accounts, once per day (or on demand).
- Importing balances into the existing mirror bookkeeping via the same code path
  as a manual dashboard sync.
- Surfacing **unknown** follower accounts to the operator as one-click onboarding
  proposals.

**Out of scope — the reader must refuse, not attempt**
- Any write to the Tradecopia DB (this whole section is `SELECT`-only).
- Reading `entities.auth_token`, `auth_token_expiry`, or any `*_key` /
  credential file (same hard rule as §7).
- **Auto-creating** mirrors silently. Unknown accounts are *proposed*; the
  operator confirms (13.6). A wrong auto-create pollutes the fleet model.
- Trusting `account_risk_configs` as firm rules: those rows are
  `*_source = 'tradecopia'` (Tradecopia's own guardrails), **not** the firm's
  real drawdown rules. TopHat's `FirmProfile` (`tophat/store/firms.py`) stays the
  sole authority on firm accounting.
- Rewriting booked history from `cash_balance_logs` (a future option, not v1).

### 13.3 File contract — `tc_observed_state.json` (tc-apply → TopHat)

Declarative snapshot of what Tradecopia currently believes, keyed by **account
name**. Symmetric with §5; the box `POST`s this to TopHat (13.7).

```json
{
  "version": 1,
  "generated_at": "2026-07-08T08:05:00-04:00",
  "tenant": "<uid>",
  "guard": {
    "goose_version": 20260521000000,
    "user_id": "9931744e-779b-4bdf-8782-1a0eebedc2ac"
  },
  "accounts": [
    {
      "name": "PAAPEX363570000064",
      "entity_id": "APEX_36357-demo",
      "entity_type": "demo",
      "connected": false,
      "balance": 50356.4,
      "realized_pnl": 0.0,
      "week_realized_pnl": 0.0,
      "balance_sod": 50356.4,
      "updated_at": "2026-05-29T22:19:47Z"
    }
  ]
}
```

Semantics:
- One entry per non-hidden `accounts` row the reader is configured to report
  (13.5). Values are copied verbatim from the DB; **no interpretation on the box
  side** — TopHat owns all policy.
- **Built 2026-07-09, beyond the sketch:** the payload also carries `groups` —
  the live copier topology, verbatim (`[{group, status, leader, followers:
  [{account, scale, replicate, contract_type}]}]`, followers sorted by account,
  groups by leader). Leader names appear here even though leader *accounts* are
  filtered from `accounts` (13.5). The server stores it in `tc_observed.json`
  and the Operations page renders it as the "Tradecopia — live mapping" table,
  badging each follower with what the next sync will do to it. Additionally the
  box's flag poll became `GET /api/ops/tc-poll?apply_at=HH:MM` so TopHat can
  display the nightly apply schedule (a box-config fact) without guessing.
- `updated_at` is `accounts.updated_at` (the freshness stamp). `connected` is the
  joined `entities.is_connected` for the row's `entity_id`.
- Same guard block as §5.1: schema version + sole `user_id`. Mismatch → the box
  emits an error status and sends **no** accounts (never a partial/guessed pull).
- `tenant` is carried from day one (§12.2), so a shared instance can fan the pull
  out per tenant without a rewrite.
- The wire body **is** the file: a host can pull it by hand and import it via the
  TopHat UI, air-gapped.

### 13.4 TopHat-side importer (this repo) — the mapping

New self-contained service in `tophat/services/` (proposed `tc_observe.py`),
touching no trading paths. For each observed account:

1. **Match by name.** `accounts.name` → the mirror whose `account_number` equals
   it, exactly (mirrors store the firm's real id in `account_number`). Zero
   matches → onboarding candidate (13.6). Multiple → error (never guess).
2. **Freshness gate.** Apply the balance only if the observation is *newer than
   what TopHat already has and recent enough to trust*:
   `observed.updated_at > mirror.last_verified` **and** age ≤ configured window
   (default **36 h**). Otherwise store the observation for display but do **not**
   book it. This prevents a six-week-stale row from resetting a mirror.
3. **Convert absolute → profit-relative.** TopHat's mirror equity is *dollars
   above the account's starting balance* (`mirrors.py` docstring), so a raw
   balance is not equity. See 13.4.1 — this is the one real subtlety.
4. **Book via the existing path.** Feed the derived equity through the same
   mechanism as a manual sync (`store.mirrors.patch_mirror(mid,
   {"sync_balance": equity}, today=…)`), which sets `equity`, bumps
   `peak = max(peak, equity)`, and stamps `last_verified`. The reverse sync is
   simply an *automated* dashboard sync — no new bookkeeping semantics. Also
   record `updated_at`/`connected` on the mirror for the staleness badge.

Column → field mapping:

| Tradecopia | TopHat mirror | Notes |
|---|---|---|
| `accounts.name` | `account_number` (match key) | exact string; also autofill on onboarding |
| `accounts.balance` | `equity` (via 13.4.1 anchor) | authoritative correction of inferred drift |
| `accounts.realized_pn_l` | `last_day_pnl` (informational) | direct booked day P&L |
| `accounts.week_realized_pn_l` | (window tracking, info) | Apex consistency cross-check |
| `accounts.updated_at` | `last_verified` + staleness badge | freshness stamp, gates the apply |
| `entities.is_connected` | staleness/health flag | shown, not booked |

#### 13.4.1 The equity-anchor subtlety (do not skip)

Mirror equity is profit-relative and the zero-point differs by phase:
**eval accounts start at $50k, funded accounts start at $0** (`mirrors.py`
header). So `equity = balance − start_anchor`, where `start_anchor` is **not a
single constant**:

- Eval 50k account, Tradecopia `balance = 50,237` → equity `= +237`.
- Funded account whose firm accounting reset → the anchor is the balance at the
  moment funding began, not $50k.

Therefore the mirror needs a **stored absolute anchor** captured once, at the
start of each phase (onboarding, and again at eval→funded activation). Recommended:
add `start_balance: float` to `MirrorAccount`, set it from the first observed
Tradecopia balance for that phase (or operator-entered), and compute
`equity = observed.balance − start_balance` thereafter. Absent an anchor, the
importer must **refuse to book** that account (surface "needs anchor") rather
than assume $50k — an assumed anchor silently corrupts the trailing floor. TC's
`realized_pn_l` / `week_realized_pn_l` are anchor-free and can feed day/week
displays immediately, independent of the equity conversion.

### 13.5 Which accounts the reader reports

- **Followers only by default.** Skip rows whose `entity_id` belongs to a
  ProjectX/Topstep (leader) entity — TopHat already has those live via API, and
  double-sourcing invites drift. Configurable, in case the operator wants TC as a
  cross-check on leader balances too.
- Skip `is_hidden = 1`.
- Report **stale** followers anyway (the six May-29 rows) — staleness is
  information the Operations page should show, not a reason to drop the row.

### 13.6 Auto-onboarding proposals (the second win)

Today `account_number` is operator-typed when a new account is bought. Instead:
an observed `accounts.name` that matches **no** mirror and sits on a follower
entity becomes a **proposed onboarding** on the Operations page — firm inferred
from `entity_id` / name prefix (recon: Apex funded = `PA…`, eval = `APEX…`),
`account_number` and `alias` pre-filled. One operator click →
`store.mirrors.create_mirror(firm, account_number=…, alias=…)`. **Never
auto-created**: a mis-inferred firm or a stray demo account would poison the
fleet model and the copier plan built from it.

### 13.7 How it rides the existing agent (no new infrastructure)

The reverse sync is one more mode on the same `copier-sync` box process and the
same box↔TopHat channel from §12.1 — no new host, port, or trust boundary:

```
TopHat  ──GET  /api/ops/tc-desired ──▶ copier-sync ──writes DB──▶ (§7 writer)
TopHat  ◀─POST /api/ops/tc-observed ── copier-sync ──reads  DB──▶ (§13 reader)
```

- New CLI mode `tc-apply observe` (read-only): open DB read-only, run the guard
  check, emit `tc_observed_state.json`, `POST` it to `/api/ops/tc-observed` with
  the box's scoped bearer token (§12.1). No app stop, no write, no relaunch.
- On a nightly `run`, the reader reuses the **same read-only snapshot** the §7.1
  preflight already opens — one DB read serves both the write-diff and the
  observed export.
- Because it is pure read, `observe` may also run **standalone and more often**
  than the write (e.g., an extra post-session pull) without any of the
  quit/relaunch machinery — cadence is a config knob, bounded only by 13.1.

### 13.8 Endpoint + surfacing (TopHat)

- `POST /api/ops/tc-observed` (box → TopHat, scoped box token, tenant-bound):
  validates guard + shape, runs 13.4, returns a per-account result
  (`booked | stale | needs_anchor | unmatched | proposed`).
- `GET /api/ops/tc-observed/last`: the last accepted snapshot for the Operations
  page — a per-follower table of **balance · day/week P&L · `updated_at` ·
  connected**, with an explicit **staleness badge** when age exceeds the window
  or `connected = false`. This is the honest presentation 13.1(2) requires: a
  balance is always shown *as of* its timestamp, never as "now".
- Discord (§12.4): the nightly status line gains a one-liner —
  `synced N followers, M stale, K proposed` — folded into the existing recap.

### 13.9 Reader test suite (no app, no live DB)

- Fixture DB from the Stage-0 schema dump with synthetic follower accounts.
- Golden: fixture rows in → exact `tc_observed_state.json` out (fresh + stale
  mixed; leader entity excluded; hidden excluded).
- Importer: matched→booked with correct anchor math; stale→not booked but stored;
  missing-anchor→`needs_anchor`, no write; unknown name→`proposed`, no mirror
  created; duplicate names→error, no write.
- Guard: wrong goose version / extra `user_id` → no accounts emitted.
- Idempotency: re-importing the same snapshot books nothing new (monotonic
  `last_verified` gate holds).

### 13.10 Hard rules for the reverse sync

1. **`SELECT` only** against Tradecopia — never open the DB writable, never take
   an exclusive lock, safe to run while the app is up (read a copied snapshot).
2. **Never** read tokens, `auth_token_expiry`, or credential/key files.
3. **Never** book a balance older than the freshness window or without a start
   anchor — display it, don't trust it.
4. **Never** auto-create a mirror; propose and let the operator confirm.
5. Carry `updated_at` + `connected` end-to-end; the UI never shows a balance
   without its as-of time.
6. Guard mismatch (schema/user) = emit error status, send no accounts.
7. `FirmProfile` remains the only authority on firm rules; TC risk configs are
   ignored.
8. Tenant-scoped from day one; a reader only reports accounts its tenant owns.

### 13.11 Stages and gates

Lightweight — read-only, so no Gate-A empirical trade proof is needed.

- **R0 — piggyback recon.** During §9 Stage 0, additionally dump `accounts`,
  `cash_balances`, `entities` (safe cols) and confirm the name↔account_number
  match rule and the anchor-capture point per firm. **Gate:** mapping recorded in
  `fixtures.md`, operator sign-off. *(Balance/name columns already confirmed
  2026-07-08; the open item is the live follower fleet once onboarded.)*
- **R1 — importer + reader against fixtures** (agent, no live anything): §13.4
  + §13.9 tests; `MirrorAccount.start_balance` anchor added. **Gate:** suite
  green; a hand-built `tc_observed_state.json` imports to the expected mirror
  state, reviewed by the operator.
- **R2 — one supervised live pull** (operator present): `tc-apply observe`
  against the real DB with the live follower fleet connected; operator confirms
  the Operations table matches the firm dashboards (balances, staleness badges,
  proposed onboardings). **Gate:** numbers agree; any mismatch → stop, fix the
  mapping, update this section.
- **R3 — scheduled daily pull**, folded into the nightly `run` (or a standalone
  pre/post-session `observe`), status in the Discord recap. **Gate:** a week of
  balances agreeing with dashboards within the freshness window, zero manual
  balance edits needed.
