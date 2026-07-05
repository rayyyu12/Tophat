# Accounts

Lifecycle and registry editor for leader accounts, plus the mirror account
roster. Pick an account, edit, Save.

## Lifecycle panel

- **Preset** fills the fields for common states; everything stays editable:

| Preset | State it sets |
|---|---|
| Eval - day 1/2/3 | Eval phase with 0/1/2 days traded |
| Funded - nuke try 1 | Fresh funded cycle, no tries yet |
| Funded - nuke try 2 (recovery) | One failed try; next fire uses the $4,200 recovery bracket |
| Funded - flips after nuke (1/5) | Nuke landed - it counts as the cycle's first winning day |
| Funded - flip 3/5 | Mid-cycle, 3 winning days banked |
| Funded - flip-only cycle (payout 2) | Cycles 2 and 4 have no nuke (odd payouts taken) |
| Payout ready / Eval passed / Blown / Retired | Terminal or parked states |

- **Winning days this cycle** is the source of truth for payout progress: at 5 the account goes payout-ready. A landed nuke counts as one of the 5.
- **Nuke tries this cycle** has no cap - the field accepts any number; retries keep using the recovery bracket.

## Balances & registry panel

- **Alias / Notes**: display name and free text.
- **Enabled in TopHat**: same switch as the Trading table.
- **Mark inactive (override broker)**: for when Topstep shows ineligible but the API still reports tradable. Accounts whose balance is at or below the MLL floor are marked inactive automatically - no override needed.
- **Exclude from analytics**: keeps this account out of Analytics counts and spend. Use for pre-project or leftover accounts.
- **Signal channel**: designates the account as a copier signal leader. It fires that fixed bracket daily and leaves the strategy fleet entirely. Use the practice account or a disposable micro eval:
  - `apex-nuke`: 32.5 / 25 pt x 2 minis
  - `apex-flip`: 16.25 / 50 pt x 1 mini
  - `apex-eval`: 30 / 10 pt x 5 minis
- **Base balance**: $50,000 for evals, $0 for funded Express. Follower firms whose funded accounts start at $50,000 (Apex, Lucid, Tradeify) need no setting - mirrors use profit-relative accounting, so their floor math is built in.
- **Sync balance**: pulls equity and peak from the live broker balance on save.

## Mirror accounts

- Register follower-firm accounts here; TopHat models them from copied leader outcomes (see Operations).
- Add one or paste several account numbers comma-separated. New mirrors start as fresh evals at the firm's copier scale (Tradeify pre-fills 0.8x).
- Row actions:
  - **Activate funded**: after the firm issues the funded account for a passed eval. Resets tracking to the funded start and restores 1.0x scale. Tradeify parks as "waiting" for a fresh leader.
  - **Pair leader**: attach a waiting funded mirror to a fresh funded Express - paired for life.
  - **Mark Paid**: confirm the firm paid a withdrawal; advances the cycle.
  - **Sync**: enter the profit/loss figure from the firm dashboard; stamps Verified.
