"""Dashboard orchestration: build the live snapshot and run the daily plan.

Reuses the existing engine / state store / registry and adds the decorrelation
scheduler + hedge-guard. Broker is pluggable (mock by default; ProjectX live).
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.mock import MockBroker
from tophat.engine import Action, Phase, account_base, decide
from tophat.services.guards import position_guard
from tophat.services.lifecycle import mark_payout_taken, reconcile, record_pending
from tophat.services.scheduler import assign_day, load_schedule, save_schedule
from tophat.services.status import infer_phase, lifecycle_label
from tophat.store.config import load_settings
from tophat.store.registry import load_registry, save_registry
from tophat.store.states import get_or_create, load_all, save_all

ET = ZoneInfo("America/New_York")


def make_broker():
    """Mock unless TOPHAT_BROKER=live and ProjectX creds are present."""
    if os.getenv("TOPHAT_BROKER", "mock").lower() == "live":
        from tophat.broker.projectx.broker import ProjectXBroker
        b = ProjectXBroker()
        b.login()
        return b, "live"
    return MockBroker(), "mock"


def _drive(broker, nq: str) -> tuple[int, str]:
    try:
        return broker.drive_direction(nq), "stream/bars"
    except Exception:
        return 0, "unavailable"


def _side(direction: int) -> str:
    return "LONG" if direction == 1 else "SHORT" if direction == -1 else "FLAT"


def _init_state(st, cfg, account) -> None:
    """First time we see an account: infer phase from balance and set its baseline
    (eval combine starts at $50k, Express funded starts at $0)."""
    st.phase = infer_phase(account.name, account.balance)
    st.base_balance = (cfg.funded_initial_balance if st.phase == Phase.FUNDED
                       else cfg.initial_balance)
    st.equity = account.balance
    st.peak_equity_eod = max(account.balance, st.base_balance)


def build_snapshot(broker, *, mode: str) -> dict:
    settings = load_settings()
    cfg = settings.to_account_config()
    registry = load_registry()
    states = load_all()
    nq = broker.resolve_nq_contract()
    drive, drive_src = _drive(broker, nq)
    today = datetime.now(ET).strftime("%Y-%m-%d")

    accounts = broker.list_accounts()

    # transient state per account (don't persist on a read)
    tradeable_states = {}
    for a in accounts:
        is_new = a.account_id not in states
        st = get_or_create(states, a.account_id)
        if is_new:
            _init_state(st, cfg, a)
        if registry.is_enabled(a.account_id) and a.can_trade and st.phase not in (
                Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
            tradeable_states[a.account_id] = st

    assignments, _ = assign_day(tradeable_states, settings, today, load_schedule())

    rows = []
    n_funded = n_eval = n_disabled = 0
    for a in accounts:
        st = states[a.account_id]
        entry = registry.entry(a.account_id)
        enabled = entry.enabled
        if not enabled:
            n_disabled += 1
        if st.phase == Phase.EVAL:
            n_eval += 1
        elif st.phase == Phase.FUNDED:
            n_funded += 1
        asg = assignments.get(a.account_id)
        dec = decide(cfg, st, drive)
        terminal = st.phase in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED)
        plan_action = (st.phase.value if terminal
                       else (asg.action if asg else ("disabled" if not enabled else dec.action.value)))
        pos = sum(int(p.get("size", 0)) for p in broker.search_open_positions(a.account_id))
        rows.append({
            "account_id": a.account_id,
            "name": entry.alias or a.name,
            "enabled": enabled,
            "simulated": a.simulated,
            "phase": st.phase.value,
            "lifecycle": lifecycle_label(cfg, st),
            "balance": round(a.balance, 2),
            "equity": round(st.equity, 2),
            "peak": round(st.peak_equity_eod, 2),
            "floor": round(min(account_base(cfg, st), st.peak_equity_eod - cfg.trailing_drawdown), 2),
            "payouts": st.payouts_taken,
            "open_position": pos,
            "plan_action": plan_action,
            "plan_side": _side(dec.plan.direction) if dec.plan and not terminal else "",
            "plan_entry": asg.entry_time if asg else "",
            "plan_contracts": dec.plan.contracts if dec.plan else 0,
            "plan_note": asg.note if asg else dec.note,
            "payout_ready": st.payout_ready,
            "pending": st.pending_label,
        })

    rows.sort(key=lambda r: (r["phase"] != "funded", r["name"]))
    return {
        "mode": mode,
        "nq_contract": nq,
        "drive": _side(drive),
        "drive_source": drive_src,
        "as_of": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "counts": {"total": len(accounts), "funded": n_funded, "eval": n_eval, "disabled": n_disabled},
        "auto_execute": settings.auto_execute,
        "accounts": rows,
    }


def toggle_account(account_id: int) -> bool:
    reg = load_registry()
    new = reg.toggle(account_id)
    save_registry(reg)
    return new


def run_session(broker, *, execute: bool, respect_times: bool = False,
                now_et: datetime | None = None) -> dict:
    """Reconcile closed trades, then fire today's due plan.

    execute=True places real orders (double-gated by settings.auto_execute) AND
    reconciles outcomes into state; otherwise it's a read-only dry-run preview.
    respect_times=True (scheduler) only fires accounts whose stagger slot has
    arrived; False (manual Execute) fires everything still due today.
    """
    settings = load_settings()
    cfg = settings.to_account_config()
    registry = load_registry()
    states = load_all()
    nq = broker.resolve_nq_contract()
    drive, drive_src = _drive(broker, nq)
    now = now_et or datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    really_execute = execute and settings.auto_execute

    accounts = broker.list_accounts()
    balance = {a.account_id: a.balance for a in accounts}

    tradeable = {}
    for a in accounts:
        if not a.can_trade or not registry.is_enabled(a.account_id):
            continue
        is_new = a.account_id not in states
        st = get_or_create(states, a.account_id)
        if is_new:
            _init_state(st, cfg, a)
        tradeable[a.account_id] = st

    # 1. Reconcile closed trades -> advance the payout cycle (live only).
    reconciled = []
    if really_execute:
        for aid, st in tradeable.items():
            if not st.pending_label:
                continue
            flat = sum(int(p.get("size", 0)) for p in broker.search_open_positions(aid)) == 0
            out = reconcile(cfg, st, balance.get(aid, st.equity), flat)
            if out:
                reconciled.append({"account_id": aid, "trade": st.last_fire_date, "outcome": out})
            if st.payout_ready and settings.auto_disable_on_payout_ready and registry.is_enabled(aid):
                registry.entry(aid).enabled = False  # surface for manual withdrawal

    assignments, sched = assign_day(tradeable, settings, today, load_schedule())

    # 2. Fire due accounts.
    results = []
    placed = 0
    for aid, st in tradeable.items():
        asg = assignments[aid]
        if asg.action in ("idle", "retire"):
            results.append({"account_id": aid, "action": asg.action, "note": asg.note})
            continue
        if st.payout_ready:
            results.append({"account_id": aid, "action": "payout_ready",
                            "note": "awaiting manual withdrawal"})
            continue
        if st.last_fire_date == today:
            results.append({"account_id": aid, "action": "done", "note": "already fired today"})
            continue
        if respect_times and asg.entry_time and hhmm < asg.entry_time:
            results.append({"account_id": aid, "action": "scheduled",
                            "note": f"fires {asg.entry_time} ET"})
            continue
        dec = decide(cfg, st, drive)
        if dec.action != Action.TRADE or not dec.plan:
            results.append({"account_id": aid, "action": dec.action.value, "note": dec.note})
            continue
        row = {"account_id": aid, "action": asg.action, "side": _side(dec.plan.direction),
               "contracts": dec.plan.contracts, "entry_time": asg.entry_time, "note": asg.note}
        if really_execute:
            ok, reason = (True, "")
            if settings.hedge_guard:
                ok, reason = position_guard(broker, aid, nq)
            if not ok:
                row["skipped"] = reason
            else:
                broker.close_contract(aid, nq)
                row["order_id"] = broker.place_bracket(aid, nq, dec.plan)
                record_pending(st, dec.plan, balance.get(aid, st.equity), today, cfg.point_value)
                placed += 1
        results.append(row)

    if really_execute:
        save_schedule(sched)
        save_all(states)
        save_registry(registry)
    return {"executed": really_execute, "drive": _side(drive), "drive_source": drive_src,
            "orders_placed": placed, "reconciled": reconciled, "results": results}


def mark_payout(account_id: int) -> dict:
    """Operator confirms a withdrawal; advance the account to its next payout cycle."""
    settings = load_settings()
    cfg = settings.to_account_config()
    states = load_all()
    st = states.get(account_id)
    if not st:
        return {"account_id": account_id, "error": "unknown account"}
    mark_payout_taken(cfg, st)
    save_all(states)
    # re-enable so it resumes trading the next cycle (unless retired)
    reg = load_registry()
    if st.phase.value != "retired":
        reg.entry(account_id).enabled = True
        save_registry(reg)
    return {"account_id": account_id, "payouts_taken": st.payouts_taken, "phase": st.phase.value}
