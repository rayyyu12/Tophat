"""Build per-trade records and NinjaTrader-style performance statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from research.backtest_common import Bracket, resolve


@dataclass(frozen=True)
class Trade:
    date: object
    direction: int  # +1 long, -1 short
    outcome: str  # win | loss | unresolved
    pnl_points: float
    pnl_dollars: float


@dataclass
class StrategyConfig:
    name: str
    bracket: Bracket
    contracts: int
    point_value: float = 20.0


def build_trades(entries, cfg: StrategyConfig, slip: float = 0.0) -> list[Trade]:
    br = cfg.bracket
    mult = cfg.contracts * cfg.point_value
    trades: list[Trade] = []
    for day, i, direction in entries:
        outcome = resolve(day, i, direction, br, slip)
        if outcome == "win":
            pts = br.target_pts
        elif outcome == "loss":
            pts = -br.stop_pts
        else:
            pts = 0.0
        trades.append(Trade(
            date=day.date,
            direction=direction,
            outcome=outcome,
            pnl_points=pts,
            pnl_dollars=pts * mult,
        ))
    return trades


@dataclass
class PerformanceReport:
    label: str
    config: StrategyConfig
    start_date: object | None = None
    end_date: object | None = None
    calendar_days: int = 0
    trading_days: int = 0
    # Performance
    total_net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    commission: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    ulcer_index: float = 0.0
    recovery_factor: float = 0.0
    expectancy: float = 0.0
    # Trades
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    even_trades: int = 0
    unresolved_trades: int = 0
    percent_profitable: float = 0.0
    avg_trade: float = 0.0
    avg_winning_trade: float = 0.0
    avg_losing_trade: float = 0.0
    ratio_avg_win_loss: float = 0.0
    max_consec_winners: int = 0
    max_consec_losers: int = 0
    largest_winning_trade: float = 0.0
    largest_losing_trade: float = 0.0
    avg_trades_per_day: float = 0.0
    equity_curve: list[float] = field(default_factory=list)


def _max_consecutive(outcomes: list[str], target: str) -> int:
    best = cur = 0
    for o in outcomes:
        if o == target:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _equity_curve(pnls: list[float], start: float) -> list[float]:
    eq = [start]
    for p in pnls:
        eq.append(eq[-1] + p)
    return eq


def _max_drawdown(equity: list[float]) -> tuple[float, float]:
    peak = equity[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for e in equity:
        peak = max(peak, e)
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
    return max_dd, max_dd_pct


def _sharpe_sortino(pnls: list[float], trades_per_year: float) -> tuple[float, float]:
    if len(pnls) < 2:
        return 0.0, 0.0
    mean = sum(pnls) / len(pnls)
    var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    scale = math.sqrt(trades_per_year) if trades_per_year > 0 else 1.0
    sharpe = (mean / std * scale) if std > 0 else 0.0

    downside = [min(0.0, p) for p in pnls]
    dmean = sum(downside) / len(downside)
    dvar = sum((p - dmean) ** 2 for p in downside) / max(len(downside) - 1, 1)
    dstd = math.sqrt(dvar) if dvar > 0 else 0.0
    sortino = (mean / dstd * scale) if dstd > 0 else 0.0
    return sharpe, sortino


def _ulcer_index(equity: list[float]) -> float:
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    sq = 0.0
    for e in equity[1:]:
        peak = max(peak, e)
        pct = ((peak - e) / peak * 100.0) if peak > 0 else 0.0
        sq += pct * pct
    return math.sqrt(sq / (len(equity) - 1))


def analyze_trades(
    trades: list[Trade],
    cfg: StrategyConfig,
    *,
    initial_capital: float = 50_000.0,
    commission_per_trade: float = 0.0,
    slip: float = 0.0,
) -> PerformanceReport:
    resolved = [t for t in trades if t.outcome in ("win", "loss")]
    unresolved = [t for t in trades if t.outcome == "unresolved"]

    pnls = [t.pnl_dollars - commission_per_trade for t in resolved]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    evens = [p for p in pnls if p == 0]

    gross_profit = sum(wins)
    gross_loss = sum(losses)
    net = sum(pnls)
    outcomes = [t.outcome for t in resolved]

    dates = [t.date for t in trades]
    start = min(dates) if dates else None
    end = max(dates) if dates else None
    trading_days = len({t.date for t in resolved})
    calendar_span = (end - start).days + 1 if start and end and start != end else trading_days
    years = max(calendar_span / 365.25, trading_days / 252.0, 1 / 252.0)
    trades_per_year = len(resolved) / years if years > 0 else len(resolved)

    equity = _equity_curve(pnls, initial_capital)
    max_dd, max_dd_pct = _max_drawdown(equity)
    sharpe, sortino = _sharpe_sortino(pnls, trades_per_year)
    ulcer = _ulcer_index(equity)

    pf = (gross_profit / abs(gross_loss)) if gross_loss < 0 else float("inf")
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    return PerformanceReport(
        label=cfg.name,
        config=cfg,
        start_date=start,
        end_date=end,
        calendar_days=calendar_span,
        trading_days=trading_days,
        total_net_profit=net,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        commission=commission_per_trade * len(resolved),
        profit_factor=pf,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        ulcer_index=ulcer,
        recovery_factor=(net / max_dd) if max_dd > 0 else float("inf"),
        expectancy=(net / len(resolved)) if resolved else 0.0,
        total_trades=len(resolved),
        winning_trades=len(wins),
        losing_trades=len(losses),
        even_trades=len(evens),
        unresolved_trades=len(unresolved),
        percent_profitable=(len(wins) / len(resolved) * 100.0) if resolved else 0.0,
        avg_trade=(net / len(resolved)) if resolved else 0.0,
        avg_winning_trade=avg_win,
        avg_losing_trade=avg_loss,
        ratio_avg_win_loss=(avg_win / abs(avg_loss)) if avg_loss < 0 else float("inf"),
        max_consec_winners=_max_consecutive(outcomes, "win"),
        max_consec_losers=_max_consecutive(outcomes, "loss"),
        largest_winning_trade=max(wins) if wins else 0.0,
        largest_losing_trade=min(losses) if losses else 0.0,
        avg_trades_per_day=(len(resolved) / trading_days) if trading_days else 0.0,
        equity_curve=equity,
    )


def _money(v: float) -> str:
    if math.isinf(v):
        return "∞"
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v:.2f}%"


def _num(v: float, digits: int = 2) -> str:
    if math.isinf(v):
        return "∞"
    return f"{v:,.{digits}f}"


def format_nt_report(r: PerformanceReport, *, initial_capital: float,
                     commission_per_trade: float, slip: float) -> str:
    br = r.config.bracket
    lines = [
        f"{'=' * 72}",
        f"Strategy Analyzer — {r.label}  (drive momentum, 1s RTH cache)",
        f"{'=' * 72}",
        f"Period          : {r.start_date} .. {r.end_date}  ({r.trading_days} trade days)",
        f"Instrument      : NQ  (${r.config.point_value:.2f}/pt)",
        f"Bracket         : {br.target_pts} pt target / {br.stop_pts} pt stop",
        f"Size            : {r.config.contracts} contract(s)",
        f"Starting capital: {_money(initial_capital)}",
        f"Commission/trade: {_money(commission_per_trade)}",
        f"Entry slippage  : {slip} pt(s)",
        "",
        "— Performance —",
        f"Total net profit          {_money(r.total_net_profit):>16}",
        f"Gross profit              {_money(r.gross_profit):>16}",
        f"Gross loss                {_money(r.gross_loss):>16}",
        f"Commission                {_money(r.commission):>16}",
        f"Profit factor             {_num(r.profit_factor):>16}",
        f"Max. drawdown             {_money(r.max_drawdown):>16}",
        f"Max. drawdown %           {_pct(r.max_drawdown_pct):>16}",
        f"Sharpe ratio              {_num(r.sharpe_ratio):>16}",
        f"Sortino ratio             {_num(r.sortino_ratio):>16}",
        f"Ulcer index               {_num(r.ulcer_index):>16}",
        f"Recovery factor           {_num(r.recovery_factor):>16}",
        f"Expectancy                {_money(r.expectancy):>16}",
        "",
        "— Trades —",
        f"Total # of trades         {r.total_trades:>16,}",
        f"Unresolved (excluded)     {r.unresolved_trades:>16,}",
        f"Percent profitable        {_pct(r.percent_profitable):>16}",
        f"# of winning trades       {r.winning_trades:>16,}",
        f"# of losing trades        {r.losing_trades:>16,}",
        f"# of even trades          {r.even_trades:>16,}",
        f"Avg. trade                {_money(r.avg_trade):>16}",
        f"Avg. winning trade        {_money(r.avg_winning_trade):>16}",
        f"Avg. losing trade         {_money(r.avg_losing_trade):>16}",
        f"Ratio avg. win / avg. loss{_num(r.ratio_avg_win_loss):>16}",
        f"Max. consec. winners      {r.max_consec_winners:>16,}",
        f"Max. consec. losers       {r.max_consec_losers:>16,}",
        f"Largest winning trade     {_money(r.largest_winning_trade):>16}",
        f"Largest losing trade      {_money(r.largest_losing_trade):>16}",
        f"Avg. # trades per day     {_num(r.avg_trades_per_day, 3):>16}",
        "",
        "Note: Metrics mirror NinjaTrader Strategy Analyzer field names.",
        "Unresolved brackets (no target/stop by session end) are excluded",
        "from trade stats, matching the Python win-rate backtests.",
    ]
    return "\n".join(lines)
