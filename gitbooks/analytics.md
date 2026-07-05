# Analytics

Live results and spend, aggregated from the trade log and the fleet state.
Estimates are labeled "est." - purchases happen outside TopHat.

## What gets counted

- An account counts only if it is visible on a live API key right now, or has recorded trades in the trade log. Stale files from removed keys or testing never inflate the numbers.
- Accounts flagged **Exclude from analytics** (Accounts page) are ignored everywhere: counts, spend, trades, payouts.
- Practice accounts and signal channels are never counted as purchased tickets.

## Stat cards

- **Net cash (est.)** = payouts banked minus tickets spent. Account balances are paper until paid out, so they are not in this number.
- **Payouts banked**: every leader payout you marked withdrawn plus every mirror payout marked paid.
- **Tickets spent (est.)**: fleet counts x list prices. Topstep is $85 all-in (no activation fee); Lucid $98; Tradeify $99; Apex $39 plus $139 activation.
- **Live eval pass rate** vs the model's 42.4%.

## Panels

- **Profit over time**: cumulative realized P&L (from reconciled trades) and banked payouts, by day.
- **Live win rates vs backtest**: per leg - live rate with a 95% confidence interval, the backtested drive rate, the coin floor (stop / (target + stop)), and the edge vs model. Legs with no live trades yet show the model only.
  - `apex-nuke`, `apex-flip`, `apex-eval` rows are the signal-channel brackets.
- **Eval funnel**: evals in progress, passed, blown, live pass rate vs model.
- **Money spent**: the spend estimate broken out per firm and item.
- **Recent payouts / Recent trades**: the raw feed, newest first. One trade row per reconciled outcome (win / loss / flat).

## Where the data comes from

- Trades append to `data/trade_log.jsonl` when a placed order reconciles - the log is append-only and never rewritten.
- Fleet counts come from persisted account states; balances prefer the live broker read when the account is visible.
