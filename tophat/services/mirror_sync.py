"""Outcome propagation and hazard detection for mirror (follower) accounts.

When a leader account reconciles a closed trade (server/service.py run_session),
every enabled mirror following that leader books the same balance delta scaled by
its copier multiplier, then advances under ITS firm's accounting rules (store/firms):
qualifying-day minimums on NET day P&L, eval consistency, trailing floor with
breakeven lock, and payout gates. Money movement and copier edits stay manual —
this module only infers state and raises hazards for the operator.

Every booked mirror day also appends one trade event (source="mirror") to the
trade log — the raw feed for Analytics' per-firm funded profit curves. Leader
aggregates (legs, realized P&L, recent trades) ignore these rows.
"""

from __future__ import annotations

import logging

from tophat.store.firms import get_firm
from tophat.store.mirrors import MirrorAccount

log = logging.getLogger("tophat.mirrors")  # [debuglog]

EPS = 1e-9
# The strategy's self-imposed stop is $1,000 at 1.0x scale; used for hazard math only.
BASE_STOP = 1_000.0


def advance_mirror(m: MirrorAccount, day_pnl: float, date: str) -> list[str]:
    """Book one day's scaled outcome into a mirror. Returns event strings."""
    events: list[str] = []
    firm = get_firm(m.firm)

    m.equity += day_pnl
    m.days_traded += 1
    m.window_profit += day_pnl
    m.last_outcome_date = date
    m.last_day_pnl = day_pnl
    if day_pnl > 0:
        m.best_day = max(m.best_day, day_pnl)
        if m.phase == "eval":
            m.eval_best_day = max(m.eval_best_day, day_pnl)
        if day_pnl >= firm.win_day_min - EPS:
            m.win_days += 1
        elif day_pnl >= 0.5 * firm.win_day_min:
            # A win that missed the qualifying bar (slippage on a thin margin) is
            # the silent-drift failure mode — surface it loudly.
            events.append(f"win day netted ${day_pnl:,.0f} < ${firm.win_day_min:,.0f}"
                          " - did NOT qualify")

    # trailing floor ratchets at EOD (propagation happens at day close)
    if m.equity <= m.floor() + EPS:
        m.phase = "blown"
        m.payout_ready = False
        events.append("BLOWN - trailing floor breached")
        return events
    m.peak = max(m.peak, m.equity)

    if m.phase == "eval":
        cons_ok = (firm.eval_consistency is None
                   or m.eval_best_day <= firm.eval_consistency * m.equity + EPS)
        if (m.equity >= firm.eval_target - EPS
                and m.days_traded >= firm.eval_min_days and cons_ok):
            m.phase = "passed"
            events.append("EVAL PASSED - unmap from copier, then activate funded")
    elif m.phase == "funded":
        if _payout_eligible(m):
            if not m.payout_ready:
                events.append(f"PAYOUT ELIGIBLE - request ${preview_payout(m):,.0f} at "
                              f"{firm.label}")
            m.payout_ready = True
    return events


def _payout_eligible(m: MirrorAccount) -> bool:
    firm = get_firm(m.firm)
    if m.win_days < firm.winning_days_required:
        return False
    if firm.payout_style == "apex-gate":
        return (m.equity >= firm.apex_min_balance - EPS
                and m.best_day <= firm.apex_consistency * max(m.window_profit, EPS) + EPS)
    return m.equity > 0


def preview_payout(m: MirrorAccount) -> float:
    """The amount the operator should request when eligible."""
    firm = get_firm(m.firm)
    if firm.payout_style == "apex-gate":
        return min(firm.payout_request, firm.payout_cap, m.equity)
    return min(0.5 * m.equity, firm.payout_cap)


def apply_leader_outcome(mirrors: dict[str, MirrorAccount], leader_id: int,
                         leader_delta: float, date: str) -> list[str]:
    """Propagate a leader's reconciled balance delta to its followers.

    Uses the leader's ACTUAL delta (includes real fees/slippage) scaled by each
    mirror's multiplier — closer to the follower's truth than the theoretical
    bracket. Skips mirrors that already booked `date` (idempotent across repeated
    reconcile passes). Returns the mirror_ids that were advanced."""
    touched: list[str] = []
    for mid, m in mirrors.items():
        if (m.leader_id != leader_id or not m.enabled or m.terminal
                or m.phase in ("waiting", "passed")):
            continue
        if m.last_outcome_date == date:
            continue
        pnl = leader_delta * m.multiplier
        phase_before = m.phase   # the phase the P&L was EARNED in (advance may flip it)
        events = advance_mirror(m, pnl, date)
        from tophat.store import trade_log
        trade_log.log_event(
            "trade", source="mirror", mirror_id=mid, firm=m.firm,
            trade_date=date, phase=phase_before,
            outcome=("win" if pnl > 0 else "loss" if pnl < 0 else "flat"),
            pnl=round(pnl, 2), equity=round(m.equity, 2))
        touched.append(mid)
        log.info("MIRROR %s (%s) booked $%+.2f from leader %s -> equity=$%.2f "
                 "phase=%s win_days=%d%s", mid, m.firm, pnl, leader_id, m.equity,
                 m.phase, m.win_days, (" | " + "; ".join(events)) if events else "")
    return touched


def activate_funded(m: MirrorAccount, *, leader_id: int | None = None) -> None:
    """Operator action: the firm issued the funded account for a passed eval.

    Resets accounting to the funded start ($0 profit), restores 1:1 copier scale
    (a 0.8x Tradeify flip would gross $136 < the $150 win-day bar), and either
    pairs immediately (leader given — Lucid twin / Apex channel) or parks in
    `waiting` for a fresh leader (Tradeify wait-for-fresh policy)."""
    if m.phase != "passed":
        raise ValueError(f"{m.mirror_id}: activate_funded requires phase=passed "
                         f"(got {m.phase})")
    m.equity = m.peak = 0.0
    m.days_traded = m.win_days = 0
    m.window_profit = m.best_day = m.eval_best_day = 0.0
    m.payout_ready = False
    m.multiplier = 1.0
    m.start_balance = None   # new phase, new anchor (reverse sync §13.4.1)
    firm = get_firm(m.firm)
    if firm.payout_style == "apex-gate":
        m.channel = "nuke"          # fresh PA opens its cycle on the nuke channel
    if leader_id is not None:
        m.leader_id = leader_id
        m.phase = "funded"
    else:
        m.leader_id = None
        m.phase = "waiting"


def pair_waiting(m: MirrorAccount, leader_id: int) -> None:
    """Pair a waiting funded mirror with a fresh leader — lockstep for life."""
    if m.phase != "waiting":
        raise ValueError(f"{m.mirror_id}: pair_waiting requires phase=waiting "
                         f"(got {m.phase})")
    m.leader_id = leader_id
    m.phase = "funded"


def mark_mirror_payout(m: MirrorAccount) -> float:
    """Operator confirms the firm paid. Books the withdrawal, resets the window,
    retires the mirror after its firm's payouts_target (4 for the clone firms,
    6 for Apex — operator decision 2026-07-06; previously apex-gate mirrors
    never retired on payout count)."""
    from tophat.store import trade_log
    amount = preview_payout(m)
    m.equity -= amount
    m.payouts_taken += 1
    trade_log.log_event("payout", mirror_id=m.mirror_id, firm=m.firm,
                        source="mirror", estimated=False, amount=round(amount, 2),
                        payout_number=m.payouts_taken)
    m.win_days = 0
    m.window_profit = 0.0
    m.best_day = 0.0
    m.payout_ready = False
    firm = get_firm(m.firm)
    if firm.payout_style == "apex-gate":
        m.channel = "nuke"          # new cycle opens with a nuke (locked policy)
    if m.payouts_taken >= firm.payouts_target:
        m.phase = "retired"
    return amount


def hazards(mirrors: dict[str, MirrorAccount], today: str) -> list[dict]:
    """Operator warnings from inferred state. Pure function, no side effects."""
    out: list[dict] = []

    def add(m: MirrorAccount, severity: str, text: str) -> None:
        out.append({"mirror_id": m.mirror_id, "alias": m.alias or m.mirror_id,
                    "firm": m.firm, "severity": severity, "text": text})

    for m in mirrors.values():
        if not m.enabled or m.terminal:
            continue
        if m.phase == "passed":
            add(m, "action", "eval passed - unmap from copier, then activate funded")
        elif m.phase == "waiting":
            add(m, "info", "waiting for a fresh funded leader - keep unmapped")
        if m.payout_ready:
            add(m, "action", f"payout eligible - request ${preview_payout(m):,.0f}, "
                             "then Mark Paid")
        if m.phase in ("eval", "funded") and m.leader_id is not None:
            stop = BASE_STOP * m.multiplier
            if m.room() <= stop + EPS:
                add(m, "danger", f"${m.room():,.0f} room to floor - one copied loss "
                                 "blows this account (consider unmapping)")
        if m.phase in ("eval", "funded") and m.leader_id is None:
            add(m, "warn", "no leader mapped - receiving no trades")
        stale = _days_between(m.last_verified, today)
        if m.phase in ("eval", "funded") and (not m.last_verified or stale > 7):
            label = f"{stale} days ago" if m.last_verified else "never"
            add(m, "warn", f"inferred balance last verified: {label} - sync against "
                           f"the firm dashboard")
    severity_rank = {"danger": 0, "action": 1, "warn": 2, "info": 3}
    out.sort(key=lambda h: severity_rank.get(h["severity"], 9))
    return out


def public_view(m: MirrorAccount) -> dict:
    """Mirror row for API responses: stored fields + computed accounting."""
    from dataclasses import asdict
    firm = get_firm(m.firm)
    return {
        **asdict(m),
        "firm_label": firm.label,
        "floor": round(m.floor(), 2),
        "room": round(m.room(), 2),
        "payout_preview": round(preview_payout(m), 2) if m.phase == "funded" else 0.0,
        "win_days_required": firm.winning_days_required,
    }


def _days_between(then: str, now: str) -> int:
    """Whole days between two YYYY-MM-DD strings; 0 on parse failure."""
    from datetime import date
    try:
        a = date.fromisoformat(then)
        b = date.fromisoformat(now)
        return (b - a).days
    except (ValueError, TypeError):
        return 0
