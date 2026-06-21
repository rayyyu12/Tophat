# TopHat CLI & flags reference

## Quick start

```powershell
pip install -r requirements.txt
copy .env.example .env    # add ProjectX username + API key

python -m tophat            # interactive dashboard (recommended)
python -m tophat probe      # test API connection
python -m tophat run        # dry-run today's plan
```

Legacy entry points still work: `python probe.py`, `python runner.py`.

---

## Where data is stored

TopHat uses **JSON text files**, not a database:

| File | What it stores |
|------|----------------|
| `data/account_states.json` | Per-account **lifecycle state**: phase (eval/funded/retired), payouts taken, winning days this cycle, nuke tries, DLL lockout, equity tracking |
| `data/account_registry.json` | **Your preferences**: which accounts are enabled/disabled, aliases, confirm-nukes setting, show-disabled toggle |

Both are human-readable and safe to back up. Delete an account's key to reset its state.

---

## How drive direction works

**Drive** = opening-range momentum: enter in the direction of the **09:30–09:45 ET** move.

| Mode | How direction is computed |
|------|---------------------------|
| **Dashboard** (`python -m tophat`) | **Real-time SignalR** market quotes push over WebSocket ([ProjectX realtime docs](https://gateway.docs.projectx.com/docs/realtime/)). `DriveTracker` records the first price at/after 09:30 and the last before 09:45, then locks LONG/SHORT/FLAT. **No REST polling.** |
| **CLI probe --drive** | One-shot REST bar fetch (`/api/History/retrieveBars`) for the 09:30–09:45 window |
| **CLI run** (no `--direction`) | Same REST bar fetch as probe |
| **Override** | `--direction long\|short\|flat` skips signal entirely |

---

## Dashboard keys (`python -m tophat`)

| Key | Action |
|-----|--------|
| `R` | Refresh account table |
| `E` | Toggle enable/disable on selected account |
| `D` | Dry-run (print plan, no orders) |
| `X` | Execute orders for all **enabled** accounts |
| `S` | Settings (confirm nukes, show disabled) |
| `Q` | Quit |

Status bar shows: NQ contract, last price (streamed), drive signal, market/user hub connection, quote count.

---

## CLI commands & flags

### `python -m tophat` (no args)

Launches the Textual dashboard with live streaming.

### `python -m tophat probe`

| Flag | Description |
|------|-------------|
| `--drive` | Also compute drive from REST bars (one request) |

### `python -m tophat run`

| Flag | Description |
|------|-------------|
| `--accounts ID [ID ...]` | Only these account IDs (default: all enabled + tradeable) |
| `--direction long\|short\|flat` | Override drive signal |
| `--execute` | Actually place bracket orders (default: dry-run) |
| `--confirm-nukes` | Allow nuke/re-nuke orders when `--execute` is set |
| `--bootstrap-phase eval\|funded` | Force phase for accounts with no saved state |

**Safety:** dry-run is default. Nukes require `--confirm-nukes` on the CLI (dashboard uses `X` after you review).

---

## Research scripts (`research/`)

| Script | Purpose |
|--------|---------|
| `python research/backtest.py` | Measure signal win rates |
| `python research/monte_carlo.py` | Pipeline EV / ruin simulation |
| `python research/pipeline.py` | Full engine walk over historical days |
| `python research/data_loader.py` | Build NQ parquet cache |

Root shims (`python backtest.py`, etc.) still forward to these.

---

## Project layout

```
tophat/           # live trading package
  engine.py       # strategy brain (phase machine, brackets)
  broker/         # ProjectX REST + orders
  streaming/      # SignalR market + user hubs
  store/          # JSON state + registry
  services/       # runner, status labels
  cli/            # Rich terminal commands
  ui/             # Textual dashboard
research/         # backtest, monte carlo, data loader
data/             # account JSON + NQ tick files + cache
docs/             # this file
```
