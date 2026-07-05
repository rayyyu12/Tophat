# Simulation

Backtest plus Monte Carlo over historical NQ data, using the exact same
decision code the live system trades with.

## How a simulation works

- One **path** = one account's whole lifetime. There is no fixed calendar: a path trades day after day until it blows, retires (4 payouts), or hits the max-days cap.
- Each simulated day draws a historical day, takes the drive direction, and resolves the chosen bracket first-touch on the real intraday price path.
- The lifecycle advances through the live engine code, so simulation and live behavior cannot diverge.
- Same parameters + same seed = identical results, always.

## Data

- The simulator runs on 1-second bars aggregated from NinjaTrader tick data (RTH, front-month rolled).
- No cache built yet = the page shows no data and Run is disabled, deliberately: accurate or nothing.
- Build the cache with `python research/reconstruction/export_sim_ticks.py` (or `--txt DIR` for NinjaTrader text exports), then restart the server.

## Setup fields

- **Lifecycle**:
  - `Full`: buy an eval, pass it, run the funded account to 4 payouts.
  - `Eval only` / `Funded only`: just that stage.
  - `Single bracket daily`: fire one fixed bracket every day with no account rules - for testing a bracket in isolation (e.g. a copier signal leg). Uses the Single bracket panel; the eval/funded panels are ignored.
- **Direction rule**: drive momentum (the live signal), always long/short, or coin flip (what results look like if the edge fully fades).
- **Same-bar ties**: when target and stop both hit inside one bar the true order is unknown. Pessimistic books the stop, optimistic the target - run both to bracket reality. Rare at 1-second resolution.
- **Firm preset** prefills that firm's costs and rules; everything stays editable.
- Date range narrows the historical day pool.

## Run fields

- **Simulated accounts (paths)**: how many account-lifetimes to run (up to 20,000).
- **Day sampling**: bootstrap (random days, order-free) or sequential (the real day order from a random start - preserves streaks and regimes).
- **Tickets to buy**: fleet stats sum this many independent simulated tickets.

## Reading the results

- All dollar figures are **net cash per ticket**: payouts banked minus ticket/activation costs. Account balance is paper until paid out.
- **Success** depends on mode: eval pass rate, reaches-a-payout, or ends profitable (single).
- **Typical ticket (median)** is what usually happens; most tickets die early and lose the ticket cost, so a negative median with a large "lucky ticket" tail is normal.
- **Fleet lines**: buying N tickets sums N independent draws - winners usually cover the duds, which is why the fleet median can be strongly positive while the single-ticket median is negative. This approximation ignores same-day correlation between accounts, so real fleets are somewhat swingier.
- **Fan chart**: net cash per ticket day by day - median line, middle 50% and middle 80% bands. Finished paths hold their final value.
- **Per-leg win rates**: measured over the historical day pool at your exact brackets, next to the coin floor (stop / (target + stop)) and the locked model rates.

## Templates

- **Save template** stores the parameter set under a name; running while a template is loaded stores the full results with it.
- Loading a template restores both parameters and stored results - no re-run needed. Same name saves over itself.
