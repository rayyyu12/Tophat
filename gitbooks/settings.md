# Settings

Strategy and automation parameters. Saved values persist server-side and apply
fleet-wide — where "fleet" means **your** fleet: every login user has their own
independent settings, API keys, accounts, and mirrors. Nothing on this page is
visible to (or affects) any other user.

## Project X API keys

- One key per ProjectX username. Keys are encrypted at rest; reveal on demand.
- Keys belong to the logged-in user only — other logins can never list or
  reveal them.
- Each username gets its own table on the Trading page and runs its own daily rules - the 1-nuke / 2-eval caps apply per key, not per fleet.
- Deleting a key removes its accounts from the dashboard (the accounts themselves are untouched).

## Account & risk

| Field | Meaning |
|---|---|
| Initial balance | Eval starting balance ($50,000) |
| Daily loss limit | Self-imposed per-day stop, $1,000 - every leg's stop equals it |
| Trailing drawdown | The MLL: $2,000 below peak, locks at the starting balance |
| Point value | $20 per NQ point per mini |

## Eval / Funded

- Eval: contracts, target (pts and $), stop, minimum days - the 5-mini 15.5/10 bracket by default.
- Funded: nuke and flip contract sizes and dollar targets, payout cap ($2,000), winning days per payout (5), payouts before retirement (4).

## Automation

| Field | Meaning |
|---|---|
| Nuke entry time | When nukes and evals fire. Entered in your local time, stored as ET |
| Flip stagger times | Flips spread across these entry slots |
| Max nukes / evals per day | Decorrelation caps, per API key |
| OCO probe time | Nightly Auto-OCO Brackets check (Sun-Thu, evening session). Place-and-cancel probe per enabled non-leader account; Discord alert names any account still on Position Brackets. Empty disables |
| Auto-execute | Arms the scheduler to place real orders. Off = dry-run plans only |
| Auto-disable on payout | Payout-ready accounts switch off until you withdraw |
| Hedge guard | Skip any account that is not flat at fire time |

## Proxy

- Routes **all ProjectX REST traffic** (auth, account reads, order placement)
  through the given proxy, so requests leave from the proxy's IP instead of the
  server's. Blank = direct connection (the default).
- Accepts any of the usual paste formats: `ip:port`, `ip:port:user:pass`,
  `user:pass@ip:port`, or a full `http://` / `socks5://` URL. Saved normalized;
  malformed values are rejected at save time.
- **Test proxy** round-trips the value in the field and shows the egress IP a
  request would leave from (works with a blank field too — it then shows the
  server's own IP).
- No handshake at fire time: saving a proxy change rebuilds the broker pool
  immediately — every credential re-logs-in through the new tunnel right then,
  so a bad proxy shows up as a table error on save, not at 09:45. Connections
  are then kept alive across the automation loop's ~30s ticks, so the entry-time
  order rides a tunnel that was in use seconds earlier.
- Applies per login user, like everything else on this page. The proxy adds its
  own network hop to every request — pick one close to the server region.

## Behavior notes

- Manual Execute (Trading page) fires regardless of auto-execute - it is an explicit, confirmed operator action.
- Entries later than entry time + 10 minutes are skipped for the day (the drive edge is measured at the entry time).
- One attempt per account per day: a rejected/failed fire consumes the day (no retries); Discord carries the reason.
- Time fields display and accept your local time zone; the backend schedules in ET.
