"""Human-readable lifecycle labels for dashboard and CLI."""

from __future__ import annotations

from tophat.engine import AccountConfig, AccountState, Phase, is_nuke_cycle


def infer_phase_from_name(name: str) -> Phase:
    """Topstep account type from the name: Express funded vs 50KTC eval."""
    upper = name.upper()
    if "EXPRESS" in upper:
        return Phase.FUNDED
    if "50KTC" in upper:
        return Phase.EVAL
    return Phase.EVAL


def is_practice(name: str) -> bool:
    """Topstep practice/sim accounts (names like 'PRAC-...') — shown but never
    traded: a practice account must not fire orders or consume an eval slot."""
    return "PRAC" in name.upper()


def infer_phase(name: str, balance: float | None = None) -> Phase:
    """Alias kept for callers that passed balance; name is the source of truth."""
    return infer_phase_from_name(name)


def sync_phase_from_name(state: AccountState, name: str, cfg: AccountConfig) -> bool:
    """Align eval/funded phase with the account name. Returns True if phase changed."""
    if state.phase in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
        return False
    inferred = infer_phase_from_name(name)
    if state.phase not in (Phase.EVAL, Phase.FUNDED) or state.phase == inferred:
        return False
    state.phase = inferred
    state.base_balance = (cfg.funded_initial_balance if inferred == Phase.FUNDED
                          else cfg.initial_balance)
    return True


def lifecycle_label(cfg: AccountConfig, state: AccountState, *, can_trade: bool = True) -> str:
    if state.phase == Phase.RETIRED:
        return "retired"
    if state.phase == Phase.PASSED:
        return "eval passed - funded incoming"
    if state.phase == Phase.BLOWN:
        return "blown"
    if not can_trade:
        return "inactive - can't trade"
    if state.phase == Phase.EVAL:
        return f"eval day {state.days_traded + 1}"
    if is_nuke_cycle(state.payouts_taken) and not state.nuke_hit_this_cycle:
        kind = "re-nuke" if state.payouts_taken >= 2 else "nuke"
        try_n = state.nuke_tries_this_cycle + 1
        return f"{kind} (try {try_n})"
    w = state.winning_days_this_cycle
    need = cfg.winning_days_required
    return f"flip {w}/{need} (payout {state.payouts_taken + 1})"


def today_plan_label(decision_action: str, plan_label: str | None = None) -> str:
    if decision_action != "trade" or not plan_label:
        return decision_action
    return plan_label
