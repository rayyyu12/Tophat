# Trading

The live control room: today's plan and execution for every leader account,
one table per API key.

## Stat cards

- **Active accounts / Funded / Eval**: count only visible rows. Closed and inactive accounts sit in the hidden footer and do not count.
- **Drive - NY open**: today's direction. Shows "forming" before the lock time, then the locked direction.

## Table columns

- **Status**: operational state - Active, Inactive (broker ineligible, operator override, or balance under the MLL floor), Blown, Passed, Retired.
- **Phase**: the program type, eval or funded. Stays put even when Status is terminal.
- **Lifecycle**: where the account is in its cycle, e.g. `eval day 2`, `nuke (try 1)`, `flip 3/5 (payout 2)`.
- **Today's plan**: the pill reads leg + side + size + entry time, e.g. `NUKE LONG x2 - 08:45`.
  - No side shown means the engine will not fire: drive still flat/forming, DLL lockout from an earlier loss, or account at the floor. The note under the pill says which.
  - `IDLE - waiting for an eval/nuke slot`: the account is queued behind the daily cap (1 nuke, 2 evals per key).
  - `PAYOUT READY`: parked until you withdraw at Topstep and press **Mark withdrawn**.
- **Enabled**: per-row switch; the header switch toggles the whole table. Disabled outranks everything except terminal states.

## Hidden rows

- Closed (blown / passed / retired) and inactive accounts fold into the footer: "N hidden - show".
- Inactive is automatic when the balance is at or below the trailing MLL floor, even if the broker API still says tradable.
- Signal-channel accounts stay visible regardless - they fire even on practice accounts.

## Buttons

- **Preview day**: dry run - computes every enabled account's plan, places nothing.
- **Execute**: fires today's plan now. Only visible while automation is disarmed; when auto-execute is on, the scheduler fires on its own and the button hides to prevent double-fires.

## Execution rules the page reflects

- One ATTEMPT per account per day (`already attempted today`): a rejected or
  failed order consumes the day the same as a placed one — no retries, because a
  later entry is off-strategy (the drive edge is measured at the entry time).
  Failures land in Discord with the reason; the account resumes tomorrow.
- Entry window = entry time + 10 min grace; missed windows skip to tomorrow (`missed`).
- Hedge guard skips accounts that are not flat (`SKIPPED`) — that also consumes the day.
- Near the floor, orders go out with no stop leg - Topstep auto-liquidation is the stop.
- Nightly at the Settings *OCO probe time* (default 22:00 ET, Sun–Thu) every
  active non-leader account gets a place-and-cancel bracket probe; accounts
  still on Topstep's "Position Brackets" setting are named in a Discord alert
  so tomorrow's fire is never the first thing that discovers them. Practice
  accounts, copy leaders, and dead accounts (balance at/below the trailing
  floor, even if the broker still says tradeable) are skipped. One probe per
  night, remembered across server restarts.
