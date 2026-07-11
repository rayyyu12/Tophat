# Operations

The daily to-do list for the external copy-trader (Tradecopia). TopHat never
touches follower firms directly - it computes what to change, you apply it.

## Today's copier plan

- A deterministic solver diffs each mirror's recorded mapping against the desired one and emits edit lines:

| Line | Meaning |
|---|---|
| MAP / MOVE | Point this mirror at a (new) leader |
| UNMAP | Stop copying (leader gone, payout pending, waiting for a slot) |
| SET_MULT | Fix the copier multiplier (eval scale vs funded 1.0x) |
| SET_CHANNEL | Apex only: move between nuke / flip / eval channels |
| ACTIVATE | Firm-side or TopHat-side step after an eval passes |
| REQUEST_PAYOUT | Request the shown amount at the firm, then Mark Paid |
| BUY | Replenish the eval pipeline (see Buy list) |

- **Mark applied in Tradecopia**: confirms you made the edits. The plan then re-solves to zero lines; any lines appearing later that day are new edits.
- "No changes today" means yesterday's mappings carry over.

## Hazards strip

Per-mirror warnings from inferred state. Color and tag = severity, nothing else:

- **Red (risk)**: one copied loss would blow this mirror - consider unmapping.
- **Orange (action)**: something for you to do - unmap a passed eval, request an eligible payout.
- **Yellow (warning)**: no leader mapped (receiving no trades), or the inferred balance has not been verified against the firm dashboard in over 7 days.
- **Gray (info)**: parked on purpose, e.g. waiting for a fresh funded leader.

## Side panels

- **Signal channels**: which leader account feeds each channel and how many followers it has today.
- **Payout queue**: mirrors eligible for withdrawal, with the amount to request.
- **Buy list**: evals to purchase to keep each firm's pipeline full (Topstep 6 **per login**, Lucid 10, Tradeify 6, Apex topped up to 8 standing while PAs < 16). Lucid buys pause automatically once three passed twins are waiting for a funded slot - fewer tickets, same profit. Steady-state this averages about 3 Topstep evals per login, 3-4 Lucid, 2 Tradeify and 4-5 Apex per week; the targets just keep the daily slots fed, they are not the weekly spend.

## Mirror mappings

- Grouped per firm. "(pending)" next to a leader means the plan assigns it but you have not marked the plan applied yet.
- **Verified** shows the last manual balance sync; "never" or stale dates surface as yellow hazards.
