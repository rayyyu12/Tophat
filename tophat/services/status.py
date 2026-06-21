"""Human-readable lifecycle labels for dashboard and CLI."""

from __future__ import annotations

from tophat.engine import AccountConfig, AccountState, Phase, is_nuke_cycle


PHASE_SPLIT = 25_000.0   # eval combine ~$50k vs funded ~$0 — balance is the clean discriminator


def infer_phase(name: str, balance: float) -> Phase:
    """Funded (Express) accounts start at $0; eval combines start at $50k. Balance is
    far more reliable than the name (which varies and can even contain 'Express')."""
    return Phase.FUNDED if balance < PHASE_SPLIT else Phase.EVAL


def lifecycle_label(cfg: AccountConfig, state: AccountState) -> str:
    if state.phase == Phase.RETIRED:
        return "retired"
    if state.phase == Phase.PASSED:
        return "eval passed — funded incoming"
    if state.phase == Phase.BLOWN:
        return "blown"
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
