"""Dashboard orchestration: build the live snapshot and run the daily plan.

Reuses the existing engine / state store / registry and adds the decorrelation
scheduler + hedge-guard. Broker is pluggable (mock by default; ProjectX live).
"""

from __future__ import annotations

import copy
import os
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.mock import MockBroker
from tophat.engine import Action, Phase, account_base, decide, is_nuke_cycle
from tophat.services.guards import position_guard
from tophat.services.lifecycle import mark_payout_taken, reconcile, record_pending
from tophat.services.scheduler import assign_day, load_schedule, save_schedule
from tophat.services.status import infer_phase_from_name, lifecycle_label, sync_phase_from_name
from tophat.store.config import load_settings
from tophat.store.registry import load_registry, save_registry
from tophat.store.states import get_or_create, load_all, save_all, state_to_dict

ET = ZoneInfo("America/New_York")

# Short-TTL snapshot cache so the 3s WebSocket push doesn't hammer the broker
# (one live snapshot = list_accounts + drive + positions×N). The UI still updates
# every WS tick; the broker is only re-read when the cache goes stale or a
# mutation (toggle / lifecycle / payout / manual run) invalidates it. Keyed per
# owner so each API key's snapshot caches independently.
SNAPSHOT_TTL = float(os.getenv("TOPHAT_SNAPSHOT_TTL", "12"))
_SNAPSHOT_CACHE: dict[str, dict] = {}
# Drive (09:30–09:45 opening range) is stable once computed for the session.
_DRIVE_CACHE: dict = {"date": "", "value": None, "src": ""}


@dataclass
class BrokerHandle:
    """One credential's broker plus the username that owns it (its dashboard table)."""
    owner: str
    broker: object | None
    mode: str
    error: str = ""


def invalidate_snapshot_cache(key: str | None = None) -> None:
    """Force the next build_snapshot to re-read the broker (call after mutations).

    `key=None` clears every owner's cache; a specific key clears just that owner."""
    if key is None:
        _SNAPSHOT_CACHE.clear()
    else:
        _SNAPSHOT_CACHE.pop(key, None)


def make_broker():
    """Mock unless TOPHAT_BROKER=live and ProjectX creds are present."""
    if os.getenv("TOPHAT_BROKER", "mock").lower() == "live":
        from tophat.broker.projectx.broker import ProjectXBroker
        b = ProjectXBroker()
        b.login()
        return b, "live"
    return MockBroker(), "mock"


def build_broker_pool() -> list[BrokerHandle]:
    """One broker per stored API credential; fall back to the env/mock broker.

    Each credential gets its own broker instance and its own dashboard table, so
    every per-account rule (one nuke/day, eval cap) applies independently per user
    — the scheduler only ever sees one owner's accounts at a time. A credential
    that fails to log in becomes an error handle rather than crashing the fleet.
    """
    from tophat.store.credentials import load_credentials
    creds = [c for c in load_credentials() if c.enabled]
    if creds:
        from tophat.broker.projectx.broker import ProjectXBroker
        from tophat.broker.projectx.client import ProjectXClient
        handles: list[BrokerHandle] = []
        for c in creds:
            try:
                client = ProjectXClient(c.username, c.api_key, base_url=c.base_url or None)
                broker = ProjectXBroker(client)
                broker.login()
                handles.append(BrokerHandle(owner=c.username, broker=broker, mode="live"))
            except Exception as exc:
                handles.append(BrokerHandle(owner=c.username, broker=None,
                                            mode="live", error=str(exc)))
        return handles
    # No stored credentials: keep the single env/mock broker working as before.
    broker, mode = make_broker()
    owner = (os.getenv("PROJECTX_USERNAME", "") if mode == "live" else "Demo") or "Account"
    return [BrokerHandle(owner=owner, broker=broker, mode=mode)]


def _drive(broker, nq: str) -> tuple[int, str]:
    """Today's drive direction, cached for the session once a definitive read lands.

    The opening-range read (09:30–09:45 ET) is stable for the rest of the day, so
    we cache the first non-zero result and stop refetching bars on every snapshot.
    """
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if _DRIVE_CACHE["date"] == today and _DRIVE_CACHE["value"]:
        return _DRIVE_CACHE["value"], _DRIVE_CACHE["src"]
    try:
        val = broker.drive_direction(nq)
    except Exception:
        return 0, "unavailable"
    if val:  # only cache a definitive directional read
        _DRIVE_CACHE.update(date=today, value=val, src="stream/bars")
    return val, "stream/bars"


def _side(direction: int) -> str:
    return "LONG" if direction == 1 else "SHORT" if direction == -1 else "FLAT"


def _effective_can_trade(broker_can_trade: bool, entry) -> bool:
    return broker_can_trade and not entry.force_inactive


def _init_state(st, cfg, account) -> None:
    """First time we see an account: infer phase from name and set its baseline."""
    st.phase = infer_phase_from_name(account.name)
    st.base_balance = (cfg.funded_initial_balance if st.phase == Phase.FUNDED
                       else cfg.initial_balance)
    st.equity = account.balance
    st.peak_equity_eod = max(account.balance, st.base_balance)


def build_snapshot(broker, *, mode: str, force: bool = False,
                   key: str | None = None, owner: str = "") -> dict:
    """Cached snapshot for one broker. Serves a recent build unless stale or
    `force`d, so the 3s WS push and frequent /api/state calls don't each hit the
    live broker. `key` (default `mode`) scopes the cache per owner."""
    now = time.monotonic()
    ck = key or mode
    c = _SNAPSHOT_CACHE.get(ck)
    if (not force and c is not None and c["mode"] == mode
            and now - c["ts"] < SNAPSHOT_TTL):
        return c["data"]
    snap = _build_snapshot(broker, mode=mode, owner=owner)
    _SNAPSHOT_CACHE[ck] = {"ts": now, "data": snap, "mode": mode}
    return snap


def _build_snapshot(broker, *, mode: str, owner: str = "") -> dict:
    settings = load_settings()
    cfg = settings.to_account_config()
    registry = load_registry()
    states = load_all()
    nq = broker.resolve_nq_contract()
    drive, drive_src = _drive(broker, nq)
    today = datetime.now(ET).strftime("%Y-%m-%d")

    accounts = broker.list_accounts()
    phase_fixed = False

    # Enabled accounts drive today's assignments; all tradeable accounts get a plan preview
    # so the UI can show the would-be plan instantly when toggling enable on.
    tradeable_states = {}
    tradeable_all = {}
    for a in accounts:
        is_new = a.account_id not in states
        st = get_or_create(states, a.account_id)
        if is_new:
            _init_state(st, cfg, a)
            phase_fixed = True
        elif sync_phase_from_name(st, a.name, cfg):
            phase_fixed = True
        entry = registry.entry(a.account_id)
        tradeable = _effective_can_trade(a.can_trade, entry)
        if tradeable and st.phase not in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
            tradeable_all[a.account_id] = st
        if entry.enabled and tradeable and st.phase not in (
                Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
            tradeable_states[a.account_id] = st

    if phase_fixed:
        save_all(states)

    sched = load_schedule()
    enabled_at = {a.account_id: registry.entry(a.account_id).enabled_at for a in accounts}
    # Separate copies: assign_day mutates slot-rotation dates, and this is a read-only
    # snapshot (never persisted), so the enabled-set and preview-set must not skew
    # each other's eval/nuke slot rotation.
    assignments, _ = assign_day(tradeable_states, settings, today, copy.deepcopy(sched), enabled_at)
    preview_assignments, _ = assign_day(tradeable_all, settings, today, copy.deepcopy(sched), enabled_at)

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
        preview = preview_assignments.get(a.account_id)
        dec = decide(cfg, st, drive)
        terminal = st.phase in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED)
        tradeable = _effective_can_trade(a.can_trade, entry)
        payout_ready = st.payout_ready and not terminal
        # Status reflects the real operational state (decoupled from program phase):
        # terminal phases surface as blown/passed/retired; otherwise active/inactive.
        status = st.phase.value if terminal else ("active" if tradeable else "inactive")
        # slot_kind: does this account compete for a (capped) eval or nuke slot? Lets the
        # UI apply the cap instantly on toggle (flips don't compete — they always run).
        if terminal or payout_ready or not tradeable:
            slot_kind = None
        elif st.phase == Phase.EVAL:
            slot_kind = "eval"
        elif (st.phase == Phase.FUNDED and is_nuke_cycle(st.payouts_taken)
              and not st.nuke_hit_this_cycle):
            slot_kind = "nuke"
        else:
            slot_kind = None
        if terminal:
            preview_action = st.phase.value
        elif payout_ready:
            preview_action = "payout_ready"
        else:
            preview_action = preview.action if preview else dec.action.value
        show_plan = dec.plan and not terminal and not payout_ready
        preview_side = _side(dec.plan.direction) if show_plan else ""
        preview_entry = "" if (terminal or payout_ready) else (preview.entry_time if preview else "")
        preview_contracts = dec.plan.contracts if show_plan else 0
        preview_note = ("awaiting withdrawal" if payout_ready
                        else (preview.note if preview else dec.note))
        if not tradeable and not terminal:
            plan_action = "idle"
            plan_side = plan_entry = ""
            plan_contracts = 0
            plan_note = ("marked inactive" if entry.force_inactive and a.can_trade
                         else "inactive — can't trade")
        elif terminal:
            plan_action = st.phase.value
            plan_side = plan_entry = plan_note = ""
            plan_contracts = 0
        elif payout_ready:
            # Parked awaiting withdrawal — do NOT show flip/nuke (mirrors run_session).
            plan_action = "payout_ready"
            plan_side = plan_entry = ""
            plan_contracts = 0
            plan_note = "awaiting withdrawal"
        elif enabled:
            plan_action = asg.action if asg else dec.action.value
            plan_side = preview_side
            plan_entry = asg.entry_time if asg else ""
            plan_contracts = preview_contracts
            plan_note = asg.note if asg else dec.note
        else:
            plan_action = "disabled"
            plan_side = plan_entry = plan_note = ""
            plan_contracts = 0
        pos = sum(int(p.get("size", 0)) for p in broker.search_open_positions(a.account_id))
        rows.append({
            "account_id": a.account_id,
            "owner": owner,
            "name": entry.alias or a.name,
            "broker_name": a.name,
            "enabled": enabled,
            "can_trade": tradeable,
            "broker_can_trade": a.can_trade,
            "force_inactive": entry.force_inactive,
            "trading_status": "active" if tradeable else "inactive",
            "status": status,
            "terminal": terminal,
            "slot_kind": slot_kind,
            "simulated": a.simulated,
            "phase": st.phase.value,
            # Program type (eval/funded) for the Phase column — stays eval/funded
            # even for terminal accounts, so Status alone carries blown/passed/retired.
            "program": infer_phase_from_name(a.name).value,
            "lifecycle": lifecycle_label(cfg, st, can_trade=tradeable),
            "balance": round(a.balance, 2),
            "equity": round(st.equity, 2),
            "peak": round(st.peak_equity_eod, 2),
            "floor": round(min(account_base(cfg, st), st.peak_equity_eod - cfg.trailing_drawdown), 2),
            "payouts": st.payouts_taken,
            "open_position": pos,
            "plan_action": plan_action,
            "plan_side": plan_side,
            "plan_entry": plan_entry,
            "plan_contracts": plan_contracts,
            "plan_note": plan_note,
            "plan_preview": {
                "plan_action": preview_action,
                "plan_side": preview_side,
                "plan_entry": preview_entry,
                "plan_contracts": preview_contracts,
                "plan_note": preview_note,
            },
            "payout_ready": st.payout_ready,
            "pending": st.pending_label,
        })

    rows.sort(key=lambda r: (r["phase"] != "funded", r["name"]))
    return {
        "mode": mode,
        "owner": owner,
        "nq_contract": nq,
        "drive": _side(drive),
        "drive_source": drive_src,
        "as_of": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "as_of_iso": datetime.now(ET).isoformat(),  # offset-aware; UI localizes to viewer tz
        "counts": {"total": len(accounts), "funded": n_funded, "eval": n_eval, "disabled": n_disabled},
        "auto_execute": settings.auto_execute,
        "max_evals_per_day": settings.max_evals_per_day,
        "max_nukes_per_day": settings.max_nukes_per_day,
        "accounts": rows,
    }


def _empty_counts() -> dict:
    return {"total": 0, "funded": 0, "eval": 0, "disabled": 0}


def build_dashboard(pool: list[BrokerHandle], *, force: bool = False) -> dict:
    """Aggregate every credential's snapshot into the dashboard payload.

    `groups` holds one per-owner table (username + that key's accounts); `accounts`
    is the flat union across all owners (kept for the stat cards and back-compat).
    """
    settings = load_settings()
    groups: list[dict] = []
    all_rows: list[dict] = []
    totals = _empty_counts()
    drive = "FLAT"
    drive_src = ""
    nq = ""
    any_live = False

    for h in pool:
        if h.mode == "live":
            any_live = True
        if h.broker is None:
            groups.append({"owner": h.owner, "mode": h.mode,
                           "error": h.error or "unavailable",
                           "accounts": [], "counts": _empty_counts()})
            continue
        try:
            snap = build_snapshot(h.broker, mode=h.mode, force=force,
                                  key=h.owner, owner=h.owner)
        except Exception as exc:                       # one bad broker ≠ dead dashboard
            groups.append({"owner": h.owner, "mode": h.mode, "error": str(exc),
                           "accounts": [], "counts": _empty_counts()})
            continue
        groups.append({
            "owner": h.owner, "mode": h.mode, "error": "",
            "accounts": snap["accounts"], "counts": snap["counts"],
            "drive": snap["drive"], "drive_source": snap["drive_source"],
            "nq_contract": snap["nq_contract"],
        })
        all_rows.extend(snap["accounts"])
        for k in totals:
            totals[k] += snap["counts"][k]
        if not drive_src:                              # drive is market-wide; take the first
            drive, drive_src, nq = snap["drive"], snap["drive_source"], snap["nq_contract"]

    return {
        "mode": "live" if any_live else "mock",
        "groups": groups,
        "accounts": all_rows,
        "counts": totals,
        "nq_contract": nq,
        "drive": drive,
        "drive_source": drive_src,
        "as_of": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "as_of_iso": datetime.now(ET).isoformat(),
        "auto_execute": settings.auto_execute,
        "max_evals_per_day": settings.max_evals_per_day,
        "max_nukes_per_day": settings.max_nukes_per_day,
    }


def run_all_sessions(pool: list[BrokerHandle], *, execute: bool,
                     respect_times: bool = False, now_et: datetime | None = None,
                     manual: bool = False) -> dict:
    """Run today's plan for every credential's broker, independently, then merge.

    Each broker's `run_session` assigns slots over only its own accounts, so the
    decorrelation caps (one nuke/day, eval cap) are scoped per user, not per fleet.
    """
    executed = False
    placed = 0
    reconciled: list[dict] = []
    results: list[dict] = []
    groups: list[dict] = []
    drive = "FLAT"
    drive_src = ""

    for h in pool:
        if h.broker is None:
            groups.append({"owner": h.owner, "error": h.error or "unavailable",
                           "results": [], "orders_placed": 0})
            continue
        r = run_session(h.broker, execute=execute, respect_times=respect_times,
                        now_et=now_et, manual=manual)
        executed = executed or r["executed"]
        placed += r["orders_placed"]
        for x in r["reconciled"]:
            reconciled.append({**x, "owner": h.owner})
        for x in r["results"]:
            x["owner"] = h.owner
            results.append(x)
        if not drive_src:
            drive, drive_src = r["drive"], r["drive_source"]
        groups.append({"owner": h.owner, "results": r["results"],
                       "orders_placed": r["orders_placed"]})

    return {
        "executed": executed,
        "orders_placed": placed,
        "drive": drive,
        "drive_source": drive_src,
        "reconciled": reconciled,
        "results": results,
        "groups": groups,
    }


def find_handle_for_account(pool: list[BrokerHandle], account_id: int) -> BrokerHandle | None:
    for h in pool:
        if h.broker is None:
            continue
        try:
            if any(a.account_id == account_id for a in h.broker.list_accounts()):
                return h
        except Exception:
            continue
    return None


def toggle_account(account_id: int) -> bool:
    reg = load_registry()
    new = reg.toggle(account_id)
    save_registry(reg)
    invalidate_snapshot_cache()
    return new


def set_account_enabled(account_id: int, enabled: bool) -> bool:
    """Idempotent enable/disable (set, not flip) — safe for rapid repeated calls."""
    reg = load_registry()
    e = reg.entry(account_id)
    if enabled and not e.enabled:
        e.enabled_at = time.time()  # off->on transition waits behind already-active accounts
    e.enabled = enabled
    save_registry(reg)
    invalidate_snapshot_cache()
    return e.enabled


def run_session(broker, *, execute: bool, respect_times: bool = False,
                now_et: datetime | None = None, manual: bool = False) -> dict:
    """Reconcile closed trades, then fire today's due plan.

    execute=True places real orders AND reconciles outcomes into state; otherwise
    it's a read-only dry-run preview.

    Two execution paths:
      - scheduler (manual=False): gated by settings.auto_execute — only fires when
        automation is armed.
      - manual operator (manual=True): an explicit, confirmed human action, so it
        fires regardless of auto_execute (still mock unless TOPHAT_BROKER=live).

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
    really_execute = execute and (manual or settings.auto_execute)

    accounts = broker.list_accounts()
    balance = {a.account_id: a.balance for a in accounts}
    phase_fixed = False

    tradeable = {}
    for a in accounts:
        entry = registry.entry(a.account_id)
        if not _effective_can_trade(a.can_trade, entry) or not entry.enabled:
            continue
        is_new = a.account_id not in states
        st = get_or_create(states, a.account_id)
        if is_new:
            _init_state(st, cfg, a)
            phase_fixed = True
        elif sync_phase_from_name(st, a.name, cfg):
            phase_fixed = True
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

    enabled_at = {aid: registry.entry(aid).enabled_at for aid in tradeable}
    assignments, sched = assign_day(tradeable, settings, today, load_schedule(), enabled_at)

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
        invalidate_snapshot_cache()  # positions/state changed — next snapshot is fresh
    elif phase_fixed:
        save_all(states)
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
    invalidate_snapshot_cache()
    return {"account_id": account_id, "payouts_taken": st.payouts_taken, "phase": st.phase.value}


_LIFECYCLE_FIELDS = {
    "phase", "days_traded", "payouts_taken", "winning_days_this_cycle",
    "nuke_tries_this_cycle", "nuke_hit_this_cycle", "payout_ready",
    "equity", "peak_equity_eod", "base_balance", "locked_out_today",
}
_REGISTRY_FIELDS = {"alias", "notes", "enabled", "force_inactive"}


def list_accounts_detail(pool: list[BrokerHandle]) -> list[dict]:
    """Full account rows for the edit-accounts page, unioned across every credential."""
    # Build snapshots first — that's what creates+persists state for new accounts —
    # then read states so freshly discovered accounts are present.
    snaps = [(h, build_snapshot(h.broker, mode=h.mode, key=h.owner, owner=h.owner))
             for h in pool if h.broker is not None]
    states = load_all()
    registry = load_registry()
    cfg = load_settings().to_account_config()
    out = []
    for h, snap in snaps:
        by_id = {r["account_id"]: r for r in snap["accounts"]}
        for a in h.broker.list_accounts():
            row = by_id.get(a.account_id)
            if not row:
                continue
            st = states[a.account_id]
            entry = registry.entry(a.account_id)
            tradeable = _effective_can_trade(a.can_trade, entry)
            out.append({
                **row,
                "owner": h.owner,
                "alias": entry.alias,
                "notes": entry.notes,
                "force_inactive": entry.force_inactive,
                "lifecycle": lifecycle_label(cfg, st, can_trade=tradeable),
                "state": state_to_dict(st),
            })
    out.sort(key=lambda r: (r["owner"], r["phase"] != "funded", r["name"]))
    return out


def update_account_lifecycle(account_id: int, patch: dict, pool: list[BrokerHandle]) -> dict:
    """Patch lifecycle state and registry fields for one account."""
    handle = find_handle_for_account(pool, account_id)
    if handle is None:
        return {"account_id": account_id, "error": "unknown account"}
    broker = handle.broker
    accounts = {a.account_id: a for a in broker.list_accounts()}
    if account_id not in accounts:
        return {"account_id": account_id, "error": "unknown account"}

    settings = load_settings()
    cfg = settings.to_account_config()
    states = load_all()
    registry = load_registry()
    is_new = account_id not in states
    st = get_or_create(states, account_id)
    if is_new:
        _init_state(st, cfg, accounts[account_id])

    for key in _LIFECYCLE_FIELDS:
        if key not in patch:
            continue
        val = patch[key]
        if key == "phase":
            st.phase = Phase(str(val))
        elif key in ("nuke_hit_this_cycle", "payout_ready", "locked_out_today"):
            setattr(st, key, bool(val))
        elif key in ("days_traded", "payouts_taken", "winning_days_this_cycle",
                     "nuke_tries_this_cycle"):
            setattr(st, key, int(val))
        else:
            setattr(st, key, float(val))

    entry = registry.entry(account_id)
    if "alias" in patch:
        entry.alias = str(patch["alias"]).strip()
    if "notes" in patch:
        entry.notes = str(patch["notes"]).strip()
    if "enabled" in patch:
        new_enabled = bool(patch["enabled"])
        if new_enabled and not entry.enabled:
            entry.enabled_at = time.time()  # transition off->on: waits behind active accounts
        entry.enabled = new_enabled
    if "force_inactive" in patch:
        entry.force_inactive = bool(patch["force_inactive"])

    if "sync_balance" in patch and patch["sync_balance"]:
        bal = accounts[account_id].balance
        st.equity = bal
        st.peak_equity_eod = max(st.peak_equity_eod, bal)

    save_all(states)
    save_registry(registry)
    invalidate_snapshot_cache()
    a = accounts[account_id]
    eff = _effective_can_trade(a.can_trade, entry)
    return {
        "account_id": account_id,
        "ok": True,
        "lifecycle": lifecycle_label(cfg, st, can_trade=eff),
        "state": state_to_dict(st),
        "enabled": entry.enabled,
        "force_inactive": entry.force_inactive,
        "broker_can_trade": a.can_trade,
        "can_trade": eff,
        "alias": entry.alias,
    }
