"""
Strategy engine for the TopHat NQ pipeline (broker-agnostic decision core).

Locked configuration (see docs/CLI.md and the master plan):
  - 50K with DLL; eval 5 minis; funded 2 minis; 4-payout nuke lifecycle.
  - Drive momentum: enter in the direction of the 09:30-09:45 ET opening move.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    EVAL = "eval"
    FUNDED = "funded"
    PASSED = "passed"     # eval cleared; Topstep deletes it & adds an Express funded acct
    BLOWN = "blown"       # hit the trailing floor / failed
    RETIRED = "retired"   # 4 payouts harvested


# Terminal phases never trade and show a status badge instead of a plan.
TERMINAL = (Phase.PASSED, Phase.BLOWN, Phase.RETIRED)


class Action(str, Enum):
    TRADE = "trade"
    HOLD = "hold"
    RETIRE = "retire"
    MANUAL = "manual"


@dataclass(frozen=True)
class AccountConfig:
    point_value: float = 20.0
    initial_balance: float = 50_000.0      # eval (combine) starting balance
    funded_initial_balance: float = 0.0    # Express funded accounts start at $0
    dll: float = 1_000.0
    trailing_drawdown: float = 2_000.0

    eval_contracts: int = 5
    eval_target_dollars: float = 3_000.0
    eval_target_pts: float = 15.5
    eval_stop_pts: float = 10.0          # = full $1,000 DLL at 5 minis (docs/STRATEGY.md §6.2)
    eval_min_days: int = 2

    # Funded sizing: nukes use the full funded size (reachable point target),
    # flips drop to 1 mini (same win prob, half the commission). See docs/STRATEGY.md.
    funded_contracts: int = 2
    flip_contracts: int = 1
    nuke_target_dollars: float = 3_200.0
    flip_target_dollars: float = 170.0
    payout_cap: float = 2_000.0

    winning_days_required: int = 5
    payouts_target: int = 4

    @property
    def floor_drop(self) -> float:
        return self.trailing_drawdown

    @property
    def nuke_recovery_target_dollars(self) -> float:
        """Day-2 gross target after a day-1 nuke loss: recover the DLL + net the nuke."""
        return self.nuke_target_dollars + self.dll

    def nuke_bracket_pts(self, attempt: int = 0) -> tuple[float, float]:
        denom = self.funded_contracts * self.point_value
        target = (self.nuke_recovery_target_dollars if attempt >= 1
                  else self.nuke_target_dollars)
        return target / denom, self.dll / denom

    def flip_bracket_pts(self) -> tuple[float, float]:
        denom = self.flip_contracts * self.point_value
        return self.flip_target_dollars / denom, self.dll / denom


@dataclass
class AccountState:
    phase: Phase = Phase.EVAL
    equity: float = 50_000.0
    peak_equity_eod: float = 50_000.0
    base_balance: float = 50_000.0    # starting balance for this account (eval 50k / funded 0)
    days_traded: int = 0
    payouts_taken: int = 0
    winning_days_this_cycle: int = 0
    nuke_tries_this_cycle: int = 0
    nuke_hit_this_cycle: bool = False
    locked_out_today: bool = False
    # --- live automation tracking ---
    payout_ready: bool = False        # 5 winning days banked; awaiting manual withdrawal
    last_fire_date: str = ""          # YYYY-MM-DD an order was placed (no double-fire)
    pending_label: str = ""           # trade awaiting outcome reconciliation
    pending_entry_balance: float = 0.0
    pending_target_dollars: float = 0.0
    pending_stop_dollars: float = 0.0
    pending_date: str = ""


@dataclass(frozen=True)
class TradePlan:
    direction: int
    contracts: int
    target_pts: float
    stop_pts: float
    label: str
    manual_stop: bool = True   # False near the floor -> no stop leg, let Topstep auto-liquidate


@dataclass(frozen=True)
class Decision:
    action: Action
    plan: TradePlan | None = None
    note: str = ""


# --- signal-account plans (docs/MULTI_FIRM_PLAN.md §3) -------------------------
# Apex-native brackets fired from designated practice/throwaway leader accounts so
# the copy-trader can distribute them to API-less follower firms. Signal accounts
# bypass decide() and the eval/funded lifecycle entirely; the bracket (including
# the stop leg — followers rely on it being copied) is always fully specified.
SIGNAL_PLAN_TEMPLATES: dict[str, TradePlan] = {
    "apex-nuke": TradePlan(direction=1, contracts=2, target_pts=32.5, stop_pts=25.0,
                           label="sig-apex-nuke"),
    "apex-flip": TradePlan(direction=1, contracts=1, target_pts=16.25, stop_pts=50.0,
                           label="sig-apex-flip"),
    "apex-eval": TradePlan(direction=1, contracts=5, target_pts=30.0, stop_pts=10.0,
                           label="sig-apex-eval"),
}


def signal_plan(key: str, drive_direction: int) -> TradePlan | None:
    """The bracket a signal channel fires today, oriented by drive. None = no fire
    (unknown key or flat drive)."""
    tpl = SIGNAL_PLAN_TEMPLATES.get(key)
    if tpl is None or drive_direction == 0:
        return None
    from dataclasses import replace
    return replace(tpl, direction=drive_direction)


def account_base(cfg: AccountConfig, state: AccountState) -> float:
    """Starting balance for this account: $50k eval combine vs $0 funded."""
    return state.base_balance


def eod_floor(cfg: AccountConfig, state: AccountState) -> float:
    """Trailing max-loss floor, locked at the account's starting balance once earned.
    Eval: trails up to 50k. Funded ($0 start): trails up to 0 (breakeven lock)."""
    return min(account_base(cfg, state), state.peak_equity_eod - cfg.trailing_drawdown)


def is_dead(cfg: AccountConfig, state: AccountState) -> bool:
    return state.equity <= eod_floor(cfg, state)


def room_to_floor(cfg: AccountConfig, state: AccountState,
                  balance: float | None = None) -> float:
    """Dollars from the live balance (or tracked equity) down to the EOD trailing floor."""
    bal = state.equity if balance is None else balance
    return bal - eod_floor(cfg, state)


def should_omit_stop(cfg: AccountConfig, state: AccountState, plan: TradePlan,
                     balance: float | None = None) -> bool:
    """True when remaining room to the trailing floor is <= the day's stop.

    Topstep's Combine MLL is breached in real time on UNREALIZED P&L (docs/PROBABILITY.md
    §0), so within one stop of the floor a manual stop would sit at/below the floor and
    fill messily. Instead we place NO manual stop and let Topstep auto-liquidate at the
    floor — a clean, certain blow. Every leg's stop is the full $1,000 DLL, so this trips
    when the account is ~$1,000 or less from blowing.
    """
    stop_dollars = plan.stop_pts * plan.contracts * cfg.point_value
    return room_to_floor(cfg, state, balance) <= stop_dollars + 1e-9


def is_nuke_cycle(payouts_taken: int) -> bool:
    return payouts_taken % 2 == 0


def decide(cfg: AccountConfig, state: AccountState, drive_direction: int) -> Decision:
    if state.phase == Phase.RETIRED:
        return Decision(Action.RETIRE, note="account retired (payouts harvested)")
    if state.phase == Phase.PASSED:
        return Decision(Action.HOLD, note="eval passed — funded account incoming")
    if state.phase == Phase.BLOWN:
        return Decision(Action.MANUAL, note="account blown (hit trailing floor)")
    if is_dead(cfg, state):
        return Decision(Action.MANUAL, note="equity at/below trailing floor - account dead")
    if state.locked_out_today:
        return Decision(Action.HOLD, note="DLL lockout in effect today")
    if drive_direction == 0:
        return Decision(Action.HOLD, note="no drive signal (flat open)")

    if state.phase == Phase.EVAL:
        return Decision(Action.TRADE, TradePlan(
            direction=drive_direction, contracts=cfg.eval_contracts,
            target_pts=cfg.eval_target_pts, stop_pts=cfg.eval_stop_pts,
            label="eval"))

    if state.payouts_taken >= cfg.payouts_target:
        return Decision(Action.RETIRE, note=f"{state.payouts_taken} payouts banked")

    nuke_cycle = is_nuke_cycle(state.payouts_taken)
    if nuke_cycle and not state.nuke_hit_this_cycle:
        # attempt 0 = base target; attempt >= 1 = wider day-2 recovery bracket
        tgt, stp = cfg.nuke_bracket_pts(state.nuke_tries_this_cycle)
        label = "renuke" if state.payouts_taken >= 2 else "nuke"
        return Decision(Action.TRADE, TradePlan(
            direction=drive_direction, contracts=cfg.funded_contracts,
            target_pts=tgt, stop_pts=stp, label=label))

    tgt, stp = cfg.flip_bracket_pts()
    return Decision(Action.TRADE, TradePlan(
        direction=drive_direction, contracts=cfg.flip_contracts,
        target_pts=tgt, stop_pts=stp, label="flip"))


def on_eval_result(cfg: AccountConfig, state: AccountState, won: bool) -> None:
    state.days_traded += 1
    win_dollars = min(cfg.eval_target_dollars / 2, cfg.eval_target_pts *
                      cfg.eval_contracts * cfg.point_value)
    if won:
        state.equity += win_dollars
    else:
        state.equity -= cfg.dll
        state.locked_out_today = True
    state.peak_equity_eod = max(state.peak_equity_eod, state.equity)
    if (state.equity - cfg.initial_balance >= cfg.eval_target_dollars
            and state.days_traded >= cfg.eval_min_days):
        state.phase = Phase.FUNDED
        state.equity = cfg.initial_balance
        state.peak_equity_eod = cfg.initial_balance
        state.days_traded = 0


def on_funded_result(cfg: AccountConfig, state: AccountState,
                     plan: TradePlan, won: bool) -> float:
    state.days_traded += 1
    withdrawn = 0.0
    # Realized win = the bracket that was actually placed (handles the wider
    # day-2 recovery nuke and the 1-mini flip without special-casing).
    realized = plan.target_pts * plan.contracts * cfg.point_value
    if plan.label in ("nuke", "renuke"):
        state.nuke_tries_this_cycle += 1
        if won:
            state.equity += realized
            state.nuke_hit_this_cycle = True
            state.winning_days_this_cycle += 1
        else:
            state.equity -= cfg.dll
            state.locked_out_today = True
    else:
        if won:
            state.equity += realized
            state.winning_days_this_cycle += 1
        else:
            state.equity -= cfg.dll
            state.locked_out_today = True
    state.peak_equity_eod = max(state.peak_equity_eod, state.equity)

    if state.winning_days_this_cycle >= cfg.winning_days_required:
        profit = state.equity - cfg.initial_balance
        if profit > 0:
            withdrawn = min(0.5 * profit, cfg.payout_cap)
            state.equity -= withdrawn
            state.payouts_taken += 1
        state.winning_days_this_cycle = 0
        state.nuke_hit_this_cycle = False
        state.nuke_tries_this_cycle = 0
        if state.payouts_taken >= cfg.payouts_target:
            state.phase = Phase.RETIRED
    return withdrawn


def start_new_day(state: AccountState) -> None:
    state.locked_out_today = False
