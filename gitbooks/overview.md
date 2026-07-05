# TopHat Overview

TopHat automates a daily NQ futures strategy across a fleet of prop-firm
accounts, and orchestrates copying it to firms that have no API.

## The strategy in one minute

- **Drive**: the direction of the NQ move from the 09:30 ET open to 09:45 ET. Locks at 09:45. Flat open = no trades that day.
- Every trade is one bracket order per account per day, entered in the drive direction.
- **Eval leg**: 5 minis, 15.5 pt target / 10 pt stop (+$1,550 / -$1,000 per day). Pass at +$3,000 with at least 2 trading days.
- **Nuke leg** (funded): 2 minis, $3,200 target / $1,000 stop. Failed tries retry with a $4,200 recovery target. Tries continue until the nuke lands or the account blows - there is no try cap.
- **Flip leg** (funded): 1 mini, $170 target / $1,000 stop. High win rate, small size.

## Account lifecycle

- Eval ($50,000 start) -> pass -> Topstep deletes it and issues a funded Express account ($0 start).
- Funded accounts run payout cycles of 5 winning days each:
  - Cycles 1 and 3: nuke first (the nuke win is the cycle's first winning day), then flips.
  - Cycles 2 and 4: flips only.
- Each payout = min($2,000, half the balance). After 4 payouts the account retires.
- **Trailing max loss (MLL)**: $2,000 below peak, locking at the starting balance once earned. Balance at or below the floor = dead. TopHat marks such accounts inactive automatically.
- Within $1,000 of the floor, orders go out with no protective stop - Topstep's auto-liquidation is the stop.

## Three kinds of accounts

| Kind | Firm | How TopHat handles it |
|---|---|---|
| Leader | Topstep (ProjectX API) | Trades directly, reconciles outcomes from balances |
| Mirror | Lucid / Tradeify / Apex (no API) | Modeled by inference: books the leader's scaled result, firm rules applied |
| Signal channel | A practice or throwaway account | Fires a fixed bracket daily for the copy-trader; no lifecycle |

## Risk and decorrelation rules

- At most 1 nuke and 2 evals fire per day per API key.
- Flips stagger across entry times so the fleet does not enter at once.
- Entries have a 10-minute grace window; later than that, the day is skipped.
- Hedge guard skips any account that is not flat at fire time.

## Time zones

- The backend schedules everything in ET (09:45 ET drive lock).
- The UI always displays your local time zone and converts on entry.
