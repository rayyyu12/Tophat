# Stage 0 fixtures — Tradecopia host recon (§4 of docs/TRADECOPIA_AUTOMATION_PLAN.md)

**Recorded 2026-07-08 from the live DB (read-only, via a copy of db+wal+shm).
Operator sign-off: PENDING — review the §3 corrections below before Stage 2 (Gate A).**

## Host facts

| Fixture | Value |
|---|---|
| DB path | `C:\Users\Rayyan Khan\AppData\Roaming\tradecopia\tradecopia-desktop.db` (+ `-wal`, `-shm` present) |
| `PRAGMA journal_mode` | `wal` |
| `goose_db_version` (MAX version_id) | `20260521000000` |
| Process name | `Tradecopia.exe` (single process; used by deploy/watchdog.py since 2026-07-07) |
| Launch exe | `C:\Program Files\Tradecopia Solutions Inc\Tradecopia\Tradecopia.exe` (verified present; no args) |
| App log | `tradecopia-desktop.log.enc` — **encrypted, unreadable**. `tradecopia-desktop-logs.db` is report/sync data only, no boot markers. → verify watches the written ROWS through a `verify_settle_s` window (default 90 s); any damage = decisive rollback. |
| Feeds (**CORRECTION 2026-07-14, goose 20260628000001**) | The 20260521 recon saw one `balance_polling` feed per account; on the UPDATED app the live `feeds` table sits **EMPTY in steady state** — app running, all entities connected, copying configured. Feeds are therefore NOT part of verify (requiring them rolled back two correct applies on 2026-07-13). The writer still deletes feed rows for touched accounts (harmless; delete-only rule stands). |
| Typical boot duration | Entity reconnect completes within ~2 min of relaunch (2026-07-13 observations). Verify's settle window (90 s) covers the boot-reconciliation risk, not feeds. |
| Tradecopia user_id | `9931744e-779b-4bdf-8782-1a0eebedc2ac` (single row in `users`, matches every `entities`/`groups` row) |

## Live account map (9 accounts, 2026-07-08)

| accounts.id | entity_id | name |
|---|---|---|
| 21502764 | topstepx-rizzbizzy786 | PRAC-V2-14624-36457565 (current leader, group "D HESITATION") |
| 24232172 | topstepx-rizzbizzy786 | 50KTC-V2-DLL-14624-20981325 |
| 24232192 | topstepx-rizzbizzy786 | 50KTC-V2-DLL-14624-11633903 |
| 51664464 | APEX_36357-demo | PAAPEX363570000064 (follower, replicate=0 'user_manual') |
| 52195673–7 | APEX_36357-demo | APEX3635700000222–226 (followers, replicate=1) |

- **Topstep names are IDENTICAL to the ProjectX names TopHat sees** — and the
  ProjectX-side `accounts.id` values are the same integers TopHat's registry
  uses (21502764 appears in both). The §5 contract stays name-keyed anyway.
- Apex/Tradovate account ids share the id space (5xxxxxxx range) — ids are
  Tradecopia-internal for those; names are the only portable key.

## §3 corrections (dump wins over the plan doc)

1. **Association-table pks**: `group_leader_accounts.id` and
   `group_follower_accounts.id` are `INTEGER PRIMARY KEY AUTOINCREMENT`, and the
   app inserts them **explicitly = the account's `accounts.id`** (verified on all
   7 rows). The writer must do the same (never let AUTOINCREMENT assign).
2. **`group_follower_accounts` has an extra column** `replication_disable_reason
   TEXT DEFAULT ''` — the UI writes `'user_manual'` when replicate is toggled
   off. Writer convention: `''` when `replicate=1`, `'user_manual'` when `0`
   (mimic the UI exactly; don't invent new enum values).
3. **`group_leader_accounts` also carries** `entity_id` and `account_name`
   (plan §3 said "row = just the pk"): populate both from `accounts`.
4. **groups flags in live data**: `disable_replication_on_reconcile=0`,
   `position_reconciler_enabled=1`, `auto_close_follower_positions=1`,
   `prevent_hedging=1` — the plan assumed all four default 1. New groups copy
   the live convention `(0,1,1,1)`.
5. **`groups.name` is operator-visible** (live: "D HESITATION"). Writer names
   new groups `TopHat <leader name>`; a reused group keeps its name.
6. **`feeds.entity_id` is declared INTEGER but stores TEXT** entity ids
   (SQLite affinity quirk). Irrelevant to the writer (delete-only), noted for
   sanity when reading.
7. **FKs exist with ON DELETE CASCADE** (groups → leader/follower rows). The
   writer does NOT rely on them (`PRAGMA foreign_keys` defaults OFF): explicit
   deletes, children before parents.
8. **Timestamps** are Go-format local strings, e.g.
   `2026-05-28 19:40:52.8462114-05:00` (7-digit fraction + offset). The writer
   emits the same shape (7-digit fraction). Whether the app tolerates other
   formats is untested — don't find out.

## Extra guard adopted (addition to §7.1 preflight)

`positions.net_pos != 0` for any account the run touches → **abort**. The DB
reflects state as of the last app write, so this is belt-and-braces on top of
the time-window rule, not a substitute. (Live check 2026-07-08: single
positions row, net_pos=0.)

## Current copier arrangement (for reference)

One group `fbeb4ec8-45df-45c7-a6f0-1e941bec26cf` ("D HESITATION"):
leader PRAC-V2-14624-36457565 → followers PAAPEX363570000064 (replicate 0,
scale 1.0) + APEX3635700000222..226 (replicate 1, scale 1.0, 'Standard').
Feeds: one `balance_polling` row per account, update_interval 2.

## R0 — reverse-sync recon additions (§13.11, 2026-07-08)

- `accounts.name` ↔ `MirrorAccount.account_number` is an **exact string
  match** (mirrors store the firm's real id; Tradecopia stores the same).
- Anchor capture points (§13.4.1): onboarding (fresh eval, days_traded=0) and
  eval→funded activation (`activate_funded` clears `start_balance`); first
  fresh observation of an untraded phase auto-captures the anchor at equity 0.
- Leader detection for the reader: `entities.type = 'projectx'` (live:
  topstep entity is `projectx`, Apex is `demo`). Firm inference for
  onboarding proposals: `entities.organization` substring — live values
  `ApexTraderFunding`, `topstepx`.
- `cash_balances.amount_sod` = start-of-day balance (informational,
  `balance_sod` in the payload); latest row per account.
- App-log timestamps unavailable (encrypted); `accounts.updated_at` is the
  §13.1 freshness stamp. Live check 2026-07-08: Topstep rows fresh same-day,
  Apex demo rows six weeks stale — exactly the §13.1 caveat.

## DDL snapshot (2026-07-08, goose 20260521000000)

Tables the writer touches (or guards on) — exact DDL is embedded in
`tests/test_tc_apply.py` and must be refreshed here + there if the schema
guard ever trips:

- `goose_db_version(id, version_id, is_applied, tstamp)`
- `accounts(id pk, entity_id, name, balance, realized_pn_l, week_realized_pn_l, created_at, updated_at, is_hidden)` — read-only
- `entities(id pk TEXT, …, user_id, auth_token, …)` — read-only, never select token columns beyond user_id/id
- `groups(id pk TEXT, name, user_id, status, created_at, updated_at, disable_replication_on_reconcile, position_reconciler_enabled, auto_close_follower_positions, prevent_hedging)`
- `group_leader_accounts(id pk = leader accounts.id, group_id, created_at, updated_at, entity_id, account_name)`
- `group_follower_accounts(id pk = follower accounts.id, group_id, entity_id, scale, contract_type, created_at, updated_at, account_name, replicate, replication_disable_reason)`
- `feeds(id pk autoinc, name, account_id, entity_id, connection_type, connection_status, update_interval, created_at, updated_at)` — delete-only
- `positions(pk (account_id,id), net_pos, …)` — read-only flat guard
