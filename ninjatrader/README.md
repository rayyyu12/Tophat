# NinjaTrader 8 — TopHat drive backtests

Two strategies that mirror `research/backtest_flip.py` and `research/backtest_nuke.py`.

## Install

1. Copy the `.cs` files into:
   ```
   Documents\NinjaTrader 8\bin\Custom\Strategies\
   ```
2. In NinjaTrader: **New → NinjaScript Editor → Compile** (F5).
3. Open an **NQ** chart (front month or continuous).
4. **RTH session template** (09:30–16:00 ET).
5. **1-minute** bars recommended (see note below).
6. Add strategy: **TopHatDriveFlip** or **TopHatDriveNuke**.

## Signal (both strategies)

| Step | Time (ET) | Time (CT) |
|------|-----------|-----------|
| Opening range | 09:30 bar open → 09:44 bar close | 08:30 → 08:44 |
| Drive lock | 09:45 | 08:45 |
| Entry fill | Next bar open (09:46 on 1-min) | 08:46 |

- **LONG** if OR close > OR open  
- **SHORT** if OR close < OR open  
- **No trade** if flat  

## Brackets

| Strategy | Default target | Default stop | Contracts |
|----------|----------------|--------------|-----------|
| **TopHatDriveFlip** | 8.5 pts (~$170 @ 1 mini) | 50 pts (~$1k) | 1 |
| **TopHatDriveNuke** | 100 pts ($4k) | 25 pts ($1k DLL) | 2 |

Strategy inputs:

- **UseLegacyBracket** (Flip): 7.5 / 50 — matches old `backtest.py` flip row  
- **UseNoDllBracket** (Nuke): 100 / 50 — 2:1 no-DLL research bracket  

## Python equivalents

From project root (requires `data/cache/nq_rth_1s.parquet`):

```powershell
python research/backtest_flip.py
python research/backtest_nuke.py
python research/backtest_flip.py --legacy-bracket
python research/backtest_nuke.py --no-dll
python research/backtest_flip.py --monthly --slip 0.25
```

## 1-minute vs 1-second

Python backtests use **1-second** RTH cache bars. NinjaTrader backtests on **1-minute** bars will differ slightly because intrabar stop/target resolution is coarser. For closer parity, use the finest bar type your NT data supports and keep RTH session times aligned.

## Coin-flip control

The Python scripts include a random-entry control baseline. NinjaTrader strategies only run the **drive** signal (no coin-flip mode).
