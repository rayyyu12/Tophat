"""Live outcome -> state advancement.

The broker BALANCE is the source of truth for equity in live trading; the engine
keeps the lifecycle COUNTERS (winning days, nuke hit, payouts). When a placed
trade closes, we reconcile by comparing the account balance to the entry balance
and advance the counters — without the engine's own equity arithmetic and without
auto-withdrawing (money movement is always a manual operator step).
"""

from __future__ import annotations

from tophat.engine import AccountConfig, AccountState, Phase, TradePlan, account_base, is_dead


def record_pending(state: AccountState, plan: TradePlan, entry_balance: float,
                   date: str, point_value: float) -> None:
    """Mark a just-placed order as awaiting outcome reconciliation."""
    state.pending_label = plan.label
    state.pending_entry_balance = entry_balance
    state.pending_target_dollars = plan.target_pts * plan.contracts * point_value
    state.pending_stop_dollars = plan.stop_pts * plan.contracts * point_value
    state.pending_date = date
    state.last_fire_date = date


def _classify(delta: float, target: float, stop: float) -> str:
    """win | loss | flat, tolerant of fees/slippage (half-way thresholds)."""
    if target > 0 and delta >= 0.5 * target:
        return "win"
    if stop > 0 and delta <= -0.5 * stop:
        return "loss"
    return "flat"   # closed near breakeven (EOD flatten / partial) — counts as a day, no win


def reconcile(cfg: AccountConfig, state: AccountState, current_balance: float,
              position_flat: bool) -> str | None:
    """Resolve a pending trade. Returns 'win'|'loss'|'flat', or None if nothing
    to reconcile or the position is still open."""
    if not state.pending_label:
        return None
    if not position_flat:
        return None  # trade still working — check again later
    delta = current_balance - state.pending_entry_balance
    outcome = _classify(delta, state.pending_target_dollars, state.pending_stop_dollars)
    label = state.pending_label

    if state.phase == Phase.EVAL:
        _advance_eval(cfg, state, outcome, current_balance)
    else:
        _advance_funded(cfg, state, label, outcome)

    state.equity = current_balance
    state.peak_equity_eod = max(state.peak_equity_eod, current_balance)
    # blown? trailing floor breached (eval or funded, not already terminal)
    if state.phase in (Phase.EVAL, Phase.FUNDED) and is_dead(cfg, state):
        state.phase = Phase.BLOWN
    # clear pending
    state.pending_label = ""
    state.pending_entry_balance = 0.0
    state.pending_target_dollars = 0.0
    state.pending_stop_dollars = 0.0
    return outcome


def _advance_funded(cfg: AccountConfig, state: AccountState, label: str, outcome: str) -> None:
    state.days_traded += 1
    won = outcome == "win"
    # Note: no locked_out_today here — reconcile runs on the *next* day's pass, so
    # yesterday's loss must not block today. Same-day double-fire is prevented by
    # last_fire_date in the scheduler.
    if label in ("nuke", "renuke"):
        state.nuke_tries_this_cycle += 1
        if won:
            state.nuke_hit_this_cycle = True
            state.winning_days_this_cycle += 1
        # 'loss'/'flat' nuke: no winning day; decide() routes the next try to recovery
    else:  # flip
        if won:
            state.winning_days_this_cycle += 1
    if state.winning_days_this_cycle >= cfg.winning_days_required:
        state.payout_ready = True   # awaits manual withdrawal -> mark_payout_taken()


def _advance_eval(cfg: AccountConfig, state: AccountState, outcome: str,
                  current_balance: float) -> None:
    state.days_traded += 1
    if (current_balance - account_base(cfg, state) >= cfg.eval_target_dollars
            and state.days_traded >= cfg.eval_min_days):
        # Eval cleared. Topstep deletes this account within ~15-30 min and adds a NEW
        # Express funded account ($0 start) — handled independently. This one is done.
        state.phase = Phase.PASSED


def mark_payout_taken(cfg: AccountConfig, state: AccountState) -> None:
    """Operator confirms a withdrawal happened; advance to the next payout cycle."""
    state.payouts_taken += 1
    state.winning_days_this_cycle = 0
    state.nuke_hit_this_cycle = False
    state.nuke_tries_this_cycle = 0
    state.payout_ready = False
    state.pending_label = ""
    if state.payouts_taken >= cfg.payouts_target:
        state.phase = Phase.RETIRED
