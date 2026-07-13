"""Dashboard orchestration: build the live snapshot and run the daily plan.

Reuses the existing engine / state store / registry and adds the decorrelation
scheduler + hedge-guard. Broker is pluggable (mock by default; ProjectX live).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.mock import MockBroker
from tophat.clock import trading_day
from tophat.engine import (
    SIGNAL_PLAN_TEMPLATES, Action, Phase, account_base, decide, eod_floor,
    is_nuke_cycle, should_omit_stop)
from tophat.services import mirror_sync
from tophat.services.guards import position_guard
from tophat.services.lifecycle import mark_payout_taken, reconcile, record_pending
from tophat.store.mirrors import load_mirrors, merge_save_mirrors
from tophat.services.scheduler import Assignment, assign_day, load_schedule, save_schedule
from tophat.services.status import (
    infer_phase_from_name, is_practice, lifecycle_label, sync_phase_from_name)
from tophat.store import tenant
from tophat.store.config import load_settings
from tophat.store import trade_log
from tophat.store.registry import load_registry, save_registry
from tophat.store.states import get_or_create, load_all, merge_save, save_all, state_to_dict

ET = ZoneInfo("America/New_York")
_trade = logging.getLogger("tophat.trade")  # [debuglog] verbose order/fill/reconcile log


def _open_size(broker, account_id: int, contract_id: str) -> int:
    """Net open contracts on `contract_id` (0 = flat). Tolerant — never raises into
    the trading path. Used to avoid closing an already-flat account (ProjectX errors)."""
    try:
        return sum(int(p.get("size", 0)) for p in broker.search_open_positions(account_id)
                   if contract_id is None or p.get("contractId") == contract_id)
    except Exception:
        return 0


def _broker_order_state(broker, account_id: int) -> None:
    """[debuglog] Log the working bracket orders + open position for an account, so we
    can see the actual fill price and the prices the stop/target orders were set at.
    Best-effort: uses only existing endpoints; never raises into the trading path."""
    if not _trade.isEnabledFor(logging.INFO):
        return
    try:
        client = getattr(broker, "client", None)
        if client is not None and hasattr(client, "search_open_orders"):
            for o in client.search_open_orders(account_id):
                _trade.info("  working order acct=%s type=%s side=%s size=%s limit=%s stop=%s status=%s id=%s",
                            account_id, o.get("type"), o.get("side"), o.get("size"),
                            o.get("limitPrice"), o.get("stopPrice"), o.get("status"), o.get("id"))
        for p in broker.search_open_positions(account_id):
            _trade.info("  position acct=%s size=%s avgPrice=%s contract=%s",
                        account_id, p.get("size"), p.get("averagePrice") or p.get("avgPrice"),
                        p.get("contractId"))
    except Exception as exc:
        _trade.debug("  order/position query failed acct=%s: %s", account_id, exc)

# Two cache layers keep the UI fast without hammering the broker:
#   - _BROKER_CACHE: raw REST reads (accounts, position sizes, contract, drive)
#     with a short TTL. Only order placement / pool changes invalidate it.
#   - _SNAPSHOT_CACHE: the computed dashboard rows (registry + states + schedule).
#     Local mutations (toggle, lifecycle edit, settings) invalidate ONLY this
#     layer; the rebuild reuses the cached broker reads, so rapid enable/disable
#     clicks never trigger REST calls (the burst that used to 429).
# Both are keyed per owner so each API key caches independently.
SNAPSHOT_TTL = float(os.getenv("TOPHAT_SNAPSHOT_TTL", "12"))
_SNAPSHOT_CACHE: dict[str, dict] = {}
_BROKER_CACHE: dict[str, dict] = {}
# Generation counters: a build that started BEFORE an invalidation must not be
# cached AFTER it - the finished build holds pre-mutation data yet would look
# fresh for a full TTL (the "toggle flips back by itself" bug).
_SNAP_GEN = 0
_BROKER_GEN = 0
# Per-owner lock so concurrent cache-misses (WS loop on the event loop + sync API
# handlers in the threadpool) coalesce into ONE broker read instead of stampeding
# /api/Account/search - the burst that 429'd a freshly added key.
_SNAPSHOT_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# Only ONE trading session may run at a time PER TENANT: the automation tick
# (worker thread) and a manual Execute (API threadpool) must never interleave, or
# both could read last_fire_date=="" and double-fire the same account. Different
# users' fleets are fully independent (separate files, separate brokers), so they
# may run concurrently — the automation loop relies on that for on-time fires.
_RUN_LOCKS: dict[str, threading.Lock] = {}


def _tenant_key() -> str:
    uid = tenant.get_user()
    return f"u{uid}" if uid is not None else "global"


def _ck(owner: str) -> str:
    """Cache key for one credential's snapshot/broker reads, scoped per tenant so
    two users' same-named ProjectX credentials can never share cache entries."""
    return f"{_tenant_key()}:{owner}"


def _run_lock() -> threading.Lock:
    with _LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(_tenant_key(), threading.Lock())
# Serializes registry load→mutate→save cycles across API handlers and sessions.
_REG_LOCK = threading.Lock()
# Drive (09:30–09:45 opening range) is stable once computed for the session.
_DRIVE_CACHE: dict = {"date": "", "value": None, "src": ""}
# The opening range is only complete at 09:45:00 ET. Reads before then are a
# forming preview and must NEVER be cached — caching a 09:30:0x reading would
# lock the day's direction to the first seconds after the open.
DRIVE_LOCK_HHMM = "09:45"


@dataclass
class BrokerHandle:
    """One credential's broker plus the username that owns it (its dashboard table)."""
    owner: str
    broker: object | None
    mode: str
    error: str = ""


def invalidate_snapshot_cache(key: str | None = None) -> None:
    """Recompute dashboard rows on the next read (call after local mutations).

    Marks entries stale instead of deleting them so the last good build stays
    available as a fallback when a live rebuild fails. Does NOT force broker
    REST reads - use invalidate_broker_cache for that. `key=None` hits every
    owner; a specific key hits just that owner."""
    global _SNAP_GEN
    with _LOCKS_GUARD:
        _SNAP_GEN += 1
        stale = (_SNAPSHOT_CACHE.values() if key is None
                 else [_SNAPSHOT_CACHE[key]] if key in _SNAPSHOT_CACHE else [])
        for c in stale:
            c["ts"] = float("-inf")


def invalidate_broker_cache(key: str | None = None) -> None:
    """Force fresh broker REST reads (after orders placed / pool rebuilt)."""
    global _BROKER_GEN
    with _LOCKS_GUARD:
        _BROKER_GEN += 1
        stale = (_BROKER_CACHE.values() if key is None
                 else [_BROKER_CACHE[key]] if key in _BROKER_CACHE else [])
        for c in stale:
            c["ts"] = float("-inf")
    invalidate_snapshot_cache(key)


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


def _drive(broker, nq: str, now: datetime | None = None) -> tuple[int, str]:
    """Today's drive direction, cached for the session once a definitive read lands.

    The opening-range read (09:30–09:45 ET) is stable for the rest of the day, so
    we cache the first non-zero result at/after 09:45 and stop refetching bars on
    every snapshot. Before 09:45 the range is still forming: return the provisional
    value for display but never cache it as the day's direction.

    `now` is injectable so run_session(now_et=...) is deterministic — without it,
    a test (or replay) running after the 16:00 CT session rollover hits the
    session-closed guard below off the wall clock and reads flat no matter what
    time it asked for.
    """
    now = now or datetime.now(ET)
    today = trading_day(now)
    # Past the late-afternoon CT session rollover the NEXT trading day's opening
    # range hasn't formed yet, so there's no drive to show. Without this, the
    # evening bar fetch would return THIS morning's 09:30-09:45 range and cache
    # it under the new trading day — a stale "LONG" showing all evening/night.
    # (The ET calendar date only differs from the trading day after the rollover.)
    if now.strftime("%Y-%m-%d") != today:
        return 0, "session closed (flat until next open)"
    if _DRIVE_CACHE["date"] == today and _DRIVE_CACHE["value"]:
        return _DRIVE_CACHE["value"], _DRIVE_CACHE["src"]
    try:
        val = broker.drive_direction(nq)
    except Exception:
        return 0, "unavailable"
    if now.strftime("%H:%M") < DRIVE_LOCK_HHMM:
        return val, f"forming (locks {DRIVE_LOCK_HHMM} ET)"
    if val:  # only cache a definitive directional read
        _DRIVE_CACHE.update(date=today, value=val, src="stream/bars")
    return val, "stream/bars"


def _side(direction: int) -> str:
    return "LONG" if direction == 1 else "SHORT" if direction == -1 else "FLAT"


def drive_public() -> dict:
    """Today's locked drive for the unauthenticated healthz endpoint. Cache-only:
    never touches a broker, returns locked=False until the 09:45 ET read lands.
    Keyed off the trading day, so it reads flat after the CT session rollover."""
    today = trading_day(datetime.now(ET))
    if _DRIVE_CACHE["date"] == today and _DRIVE_CACHE["value"]:
        return {"drive": _side(_DRIVE_CACHE["value"]),
                "drive_source": _DRIVE_CACHE["src"], "locked": True}
    return {"drive": "", "drive_source": "", "locked": False}


def _plus_minutes(hhmm: str, minutes: int) -> str:
    """'09:45' + 10 -> '09:55' (same-day wraparound clamped mod 24h)."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return hhmm
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _effective_can_trade(broker_can_trade: bool, entry) -> bool:
    return broker_can_trade and not entry.force_inactive


def _below_mll(cfg, st, balance: float) -> bool:
    """Auto-inactive detection: the live balance is at/below the trailing MLL
    floor, so the firm won't let this account trade even if the API still says
    canTrade=true. Only meaningful for accounts still in a trading phase."""
    if st.phase not in (Phase.EVAL, Phase.FUNDED):
        return False
    return balance <= eod_floor(cfg, st)


def _auto_mark_blown(cfg, st, balance: float, account_id: int) -> bool:
    """MLL is the source of truth for blown (operator rule, 2026-07-12): a live
    balance at/below the trailing floor IS a dead account, whatever canTrade
    says — flip the phase here so counts, analytics and the replenishment
    pipeline all see it without an operator step. Returns True if flipped
    (caller persists). Two deliberate exceptions:
      - a pending TopHat trade defers to reconcile(), which books the blown
        trade's loss to the trade log and mirrors BEFORE flipping the phase
        itself (the 2026-07-09 orphaned-reconcile fix);
      - a DLL-only red day never trips this — the day loss sits above the
        trailing floor, and any intraday canTrade=false clears by next session.
    False positives can only come from wrong peak/base data; fixing the data
    (or the manual phase override in Accounts) un-blows the account."""
    if st.pending_label or not _below_mll(cfg, st, balance):
        return False
    floor = eod_floor(cfg, st)   # before the flip (eod_floor reads phase-agnostic fields)
    st.phase = Phase.BLOWN
    _trade.warning("AUTO-BLOWN acct=%s — balance $%.2f at/below MLL floor $%.2f "
                   "(broker canTrade disregarded)", account_id, balance, floor)
    return True


def _ratchet_peak_eod(cfg, st, balance: float, today: str, account_id: int) -> bool:
    """Trail the MLL floor even for trades TopHat did NOT place (manual wins,
    signal fires, drift): ProjectX returns no MLL/floor field, and Topstep
    raises the trailing floor off the END-OF-DAY balance — reconcile() only
    covers TopHat's own trades. So track the last balance seen each trading
    day, and on the first sighting of a NEW day fold the prior day's close
    into peak_equity_eod. Deliberately never ratchets intraday: a mid-day
    high that fades by the close must not raise the floor (a too-high floor
    would false-blow live accounts, and blown is terminal). Returns True
    when state changed (caller persists)."""
    changed = False
    if (st.phase in (Phase.EVAL, Phase.FUNDED)
            and st.last_seen_trading_day and st.last_seen_trading_day != today
            and st.last_seen_balance > st.peak_equity_eod):
        _trade.info("EOD FLOOR RATCHET acct=%s — peak $%.2f -> $%.2f (close of %s); "
                    "floor now $%.2f", account_id, st.peak_equity_eod,
                    st.last_seen_balance, st.last_seen_trading_day,
                    min(st.base_balance, st.last_seen_balance - cfg.trailing_drawdown))
        st.peak_equity_eod = st.last_seen_balance
        changed = True
    if st.last_seen_trading_day != today or st.last_seen_balance != balance:
        st.last_seen_trading_day = today
        st.last_seen_balance = balance
        changed = True
    return changed


def _init_state(st, cfg, account) -> None:
    """First time we see an account: infer phase from name and set its baseline."""
    st.phase = infer_phase_from_name(account.name)
    st.base_balance = (cfg.funded_initial_balance if st.phase == Phase.FUNDED
                       else cfg.initial_balance)
    st.equity = account.balance
    st.peak_equity_eod = max(account.balance, st.base_balance)


def _fresh(c: dict | None, mode: str) -> bool:
    return (c is not None and c["mode"] == mode
            and time.monotonic() - c["ts"] < SNAPSHOT_TTL)


def _read_broker(broker, ck: str, *, force: bool = False) -> dict:
    """Cached raw broker reads for one credential: accounts, open-position sizes,
    the active NQ contract, and the drive read. The ONLY place snapshot builds
    touch REST, so everything downstream can rebuild for free."""
    c = _BROKER_CACHE.get(ck)
    if not force and c is not None and time.monotonic() - c["ts"] < SNAPSHOT_TTL:
        return c
    gen0 = _BROKER_GEN
    nq = broker.resolve_nq_contract()
    drive, drive_src = _drive(broker, nq)
    accounts = broker.list_accounts()
    positions = {a.account_id: sum(int(p.get("size", 0))
                                   for p in broker.search_open_positions(a.account_id))
                 for a in accounts}
    c = {"ts": time.monotonic(), "accounts": accounts, "positions": positions,
         "nq": nq, "drive": drive, "drive_src": drive_src}
    # An order may have been placed while we were reading; don't pin stale reads.
    if _BROKER_GEN == gen0:
        _BROKER_CACHE[ck] = c
    return c


def build_snapshot(broker, *, mode: str, force: bool = False,
                   key: str | None = None, owner: str = "") -> dict:
    """Cached snapshot for one broker. Serves a recent build unless stale or
    `force`d, so the 3s WS push and frequent /api/state calls don't each hit the
    live broker. `key` (default `mode`) scopes the cache per owner. If a rebuild
    fails (broker 429/timeout), the last good snapshot is served instead so a
    transient blip never blanks a table that was just showing data."""
    ck = key or mode
    if not force and _fresh(_SNAPSHOT_CACHE.get(ck), mode):
        return _SNAPSHOT_CACHE[ck]["data"]
    with _LOCKS_GUARD:
        lock = _SNAPSHOT_LOCKS.setdefault(ck, threading.Lock())
    with lock:
        # Double-check: a thread ahead of us may have just built it. Honor that even
        # for force=True - a build from microseconds ago is as fresh as one we'd do.
        if _fresh(_SNAPSHOT_CACHE.get(ck), mode):
            return _SNAPSHOT_CACHE[ck]["data"]
        gen0 = _SNAP_GEN
        try:
            snap = _build_snapshot(broker, mode=mode, owner=owner, ck=ck, force=force)
        except Exception:
            last = _SNAPSHOT_CACHE.get(ck)
            if last is not None and last["mode"] == mode:
                return last["data"]
            raise
        # A mutation may have landed while we were building; caching this build
        # would pin pre-mutation rows for a full TTL. Serve it, don't cache it.
        if _SNAP_GEN == gen0:
            _SNAPSHOT_CACHE[ck] = {"ts": time.monotonic(), "data": snap, "mode": mode}
        _persist_account_names(ck, snap)
        return snap


_NAMES_WRITTEN: dict[str, dict[str, str]] = {}
_NAMES_LOCK = threading.Lock()


def _persist_account_names(ck: str, snap: dict) -> None:
    """Write-through {account_id: broker name} beside the state files so offline
    tooling can label accounts without auth or broker calls (also feeds
    ops_recap's labels). Merges across credentials; only touches disk when a
    name changed. Best-effort — a cache write must never break a snapshot."""
    try:
        names = {str(r["account_id"]): (r.get("broker_name") or r.get("name") or "")
                 for r in snap.get("accounts", [])}
        names = {k: v for k, v in names.items() if v}
        if not names or _NAMES_WRITTEN.get(ck) == names:
            return
        from tophat.store.atomic import atomic_write_text
        from tophat.store.paths import ACCOUNT_NAMES_FILE
        with _NAMES_LOCK:
            path = tenant.resolve(ACCOUNT_NAMES_FILE)
            try:
                merged = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                merged = {}
            if all(merged.get(k) == v for k, v in names.items()):
                _NAMES_WRITTEN[ck] = names
                return
            merged.update(names)
            atomic_write_text(path, json.dumps(merged, indent=1, sort_keys=True))
            _NAMES_WRITTEN[ck] = names
    except Exception:
        pass


def _build_snapshot(broker, *, mode: str, owner: str = "",
                    ck: str = "", force: bool = False) -> dict:
    settings = load_settings()
    cfg = settings.to_account_config()
    registry = load_registry()
    states = load_all()
    reads = _read_broker(broker, ck or mode, force=force)
    nq = reads["nq"]
    drive, drive_src = reads["drive"], reads["drive_src"]
    # Trading day (rolls ~4 PM CT), not the ET calendar date: after the session
    # turns over the previous day's nuke/eval slot stamps are stale, so the slot
    # frees for the next session instead of showing "waiting" until midnight ET.
    today = trading_day(datetime.now(ET))

    accounts = reads["accounts"]
    positions = reads["positions"]
    changed_ids: list[int] = []

    # Enabled accounts drive today's assignments; all tradeable accounts get a plan preview
    # so the UI can show the would-be plan instantly when toggling enable on.
    tradeable_states = {}
    tradeable_all = {}
    for a in accounts:
        is_new = a.account_id not in states
        st = get_or_create(states, a.account_id)
        if is_new:
            _init_state(st, cfg, a)
            changed_ids.append(a.account_id)
        elif sync_phase_from_name(st, a.name, cfg):
            changed_ids.append(a.account_id)
        if not is_practice(a.name):
            # Ratchet FIRST so the blown check judges against the current floor.
            if _ratchet_peak_eod(cfg, st, a.balance, today, a.account_id):
                changed_ids.append(a.account_id)
            if _auto_mark_blown(cfg, st, a.balance, a.account_id):
                changed_ids.append(a.account_id)
        entry = registry.entry(a.account_id)
        tradeable = (_effective_can_trade(a.can_trade, entry)
                     and not is_practice(a.name)
                     and not _below_mll(cfg, st, a.balance))
        if tradeable and st.phase not in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
            tradeable_all[a.account_id] = st
        if entry.enabled and tradeable and st.phase not in (
                Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
            tradeable_states[a.account_id] = st

    if changed_ids:
        # Merge-save just the new/fixed entries: a full save_all here could clobber
        # a fire/reconcile the session runner persisted after our load_all above.
        merge_save(states, changed_ids)

    sched = load_schedule()
    enabled_at = {a.account_id: registry.entry(a.account_id).enabled_at for a in accounts}
    # Separate copies: assign_day mutates slot-rotation dates, and this is a read-only
    # snapshot (never persisted), so the enabled-set and preview-set must not skew
    # each other's eval/nuke slot rotation.
    assignments, _ = assign_day(tradeable_states, settings, today,
                                copy.deepcopy(sched), enabled_at, owner=owner)
    preview_assignments, _ = assign_day(tradeable_all, settings, today,
                                        copy.deepcopy(sched), enabled_at, owner=owner)

    rows = []
    n_funded = n_eval = n_disabled = 0
    for a in accounts:
        st = states[a.account_id]
        entry = registry.entry(a.account_id)
        enabled = entry.enabled
        practice = is_practice(a.name)
        if not enabled:
            n_disabled += 1
        if practice:
            pass          # not a strategy ticket - stays out of the fleet counts
        elif st.phase == Phase.EVAL:
            n_eval += 1
        elif st.phase == Phase.FUNDED:
            n_funded += 1
        asg = assignments.get(a.account_id)
        preview = preview_assignments.get(a.account_id)
        dec = decide(cfg, st, drive)
        terminal = st.phase in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED)
        mll_dead = _below_mll(cfg, st, a.balance)
        tradeable = (_effective_can_trade(a.can_trade, entry) and not practice
                     and not mll_dead)
        payout_ready = st.payout_ready and not terminal
        # Status reflects the real operational state (decoupled from program phase):
        # terminal phases surface as blown/passed/retired; otherwise active/inactive.
        # Practice accounts are always shown as active (they just never fire orders).
        display_active = tradeable or (practice and _effective_can_trade(a.can_trade, entry))
        status = st.phase.value if terminal else ("active" if display_active else "inactive")
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
        def plan_when_enabled(assignment):
            """(action, side, entry, contracts, note) this account runs when enabled.

            Shared by the live plan AND the preview so a disabled row's preview is
            exactly what enabling it will show - including signal/practice/inactive
            states - and the UI's optimistic toggle never has to be corrected."""
            if terminal:
                return st.phase.value, "", "", 0, ""
            if payout_ready:
                # Parked awaiting withdrawal - never flip/nuke (mirrors run_session).
                return "payout_ready", "", "", 0, "awaiting withdrawal"
            if entry.signal_plan and _effective_can_trade(a.can_trade, entry):
                # Signal channel: fires its designated bracket for the copier - shown
                # even on practice accounts (which are otherwise never traded).
                tpl = SIGNAL_PLAN_TEMPLATES.get(entry.signal_plan)
                return ("signal", _side(drive) if drive else "", settings.nuke_entry_time,
                        tpl.contracts if tpl else 0,
                        f"signal channel - {entry.signal_plan}")
            if not tradeable:
                note = ("practice account - not traded" if practice
                        else "balance at/below MLL floor" if mll_dead
                        else "marked inactive" if entry.force_inactive and a.can_trade
                        else "inactive - can't trade")
                return "idle", "", "", 0, note
            side = _side(dec.plan.direction) if dec.plan else ""
            contracts = dec.plan.contracts if dec.plan else 0
            if assignment:
                # A slot was assigned but the engine won't trade (DLL lockout,
                # flat drive, dead) - the pill loses its side, so carry the
                # engine's reason instead of the bare slot note.
                note = assignment.note
                if dec.action != Action.TRADE and dec.note:
                    note = dec.note
                return (assignment.action, side, assignment.entry_time,
                        contracts, note)
            return dec.action.value, side, "", contracts, dec.note

        (preview_action, preview_side, preview_entry,
         preview_contracts, preview_note) = plan_when_enabled(preview)
        # Precedence: terminal > payout_ready > disabled > signal > inactive > plan.
        # Disabled (operator turned it off) outranks inactive (broker can't trade) so
        # a disabled+inactive account reads "Disabled", not "Idle".
        if enabled or terminal or payout_ready:
            (plan_action, plan_side, plan_entry,
             plan_contracts, plan_note) = plan_when_enabled(asg)
        else:
            plan_action, plan_side, plan_entry, plan_contracts, plan_note = (
                "disabled", "", "", 0, "")
        pos = positions.get(a.account_id, 0)
        rows.append({
            "account_id": a.account_id,
            "owner": owner,
            "name": entry.alias or a.name,
            "broker_name": a.name,
            "enabled": enabled,
            "can_trade": display_active,
            "broker_can_trade": a.can_trade,
            "force_inactive": entry.force_inactive,
            "exclude_analytics": entry.exclude_analytics,
            "signal_plan": entry.signal_plan,
            "practice": practice,
            "trading_status": "active" if display_active else "inactive",
            "status": status,
            "terminal": terminal,
            "slot_kind": slot_kind,
            "simulated": a.simulated,
            "phase": st.phase.value,
            # Program type (eval/funded) for the Phase column — stays eval/funded
            # even for terminal accounts, so Status alone carries blown/passed/retired.
            "program": infer_phase_from_name(a.name).value,
            "lifecycle": "practice" if practice else lifecycle_label(cfg, st, can_trade=tradeable),
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
                                  key=_ck(h.owner), owner=h.owner)
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

    # One session pass at a time per tenant: a manual Execute racing an automation
    # tick would let both observe last_fire_date=="" and double-fire the same accounts.
    with _run_lock():
        for h in pool:
            if h.broker is None:
                groups.append({"owner": h.owner, "error": h.error or "unavailable",
                               "results": [], "orders_placed": 0})
                continue
            r = run_session(h.broker, execute=execute, respect_times=respect_times,
                            now_et=now_et, manual=manual, owner=h.owner)
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


def account_names_and_balances(
        pool: list[BrokerHandle]) -> tuple[dict[int, str], dict[int, float]]:
    """account_id -> (display name, live balance) across every credential (cached
    snapshots). Names label mirror/leader references; balances let Analytics show
    live funded equity instead of possibly stale tracked equity."""
    names: dict[int, str] = {}
    balances: dict[int, float] = {}
    for h in pool:
        if h.broker is None:
            continue
        try:
            snap = build_snapshot(h.broker, mode=h.mode, key=_ck(h.owner), owner=h.owner)
        except Exception:
            continue
        for r in snap["accounts"]:
            names[r["account_id"]] = r["name"]
            balances[r["account_id"]] = r["balance"]
    return names, balances


def account_names(pool: list[BrokerHandle]) -> dict[int, str]:
    """account_id -> display name across every credential (cached snapshots)."""
    return account_names_and_balances(pool)[0]


def find_handle_for_account(pool: list[BrokerHandle], account_id: int) -> BrokerHandle | None:
    for h in pool:
        if h.broker is None:
            continue
        try:
            # Use the cached snapshot rows, not a fresh list_accounts() per broker.
            snap = build_snapshot(h.broker, mode=h.mode, key=_ck(h.owner), owner=h.owner)
            if any(r["account_id"] == account_id for r in snap["accounts"]):
                return h
        except Exception:
            continue
    return None


def toggle_account(account_id: int) -> bool:
    with _REG_LOCK:
        reg = load_registry()
        new = reg.toggle(account_id)
        save_registry(reg)
    invalidate_snapshot_cache()
    return new


def set_account_enabled(account_id: int, enabled: bool) -> bool:
    """Idempotent enable/disable (set, not flip) - safe for rapid repeated calls."""
    with _REG_LOCK:
        reg = load_registry()
        e = reg.entry(account_id)
        if enabled and not e.enabled:
            e.enabled_at = time.time()  # off->on transition waits behind already-active accounts
        e.enabled = enabled
        save_registry(reg)
    invalidate_snapshot_cache()
    return e.enabled


def set_accounts_enabled(ids: list[int], enabled: bool) -> int:
    """Bulk idempotent enable/disable - one registry write for a whole table."""
    now = time.time()
    with _REG_LOCK:
        reg = load_registry()
        for aid in ids:
            e = reg.entry(aid)
            if enabled and not e.enabled:
                e.enabled_at = now
            e.enabled = enabled
        save_registry(reg)
    invalidate_snapshot_cache()
    return len(ids)


def _fire_signal(broker, aid: int, st, plan_key: str, drive: int, settings, states,
                 balance: dict, nq: str, today: str, hhmm: str,
                 respect_times: bool, really_execute: bool) -> tuple[dict, bool]:
    """Fire one signal channel's bracket (docs/MULTI_FIRM_PLAN.md §3).

    Same guards as a strategy fire (one/day, entry window, hedge guard, contained
    failures) but the bracket comes from engine.signal_plan, not decide() — signal
    accounts have no lifecycle. Returns (result_row, placed?)."""
    from tophat.engine import signal_plan
    entry_time = settings.nuke_entry_time
    if st.last_fire_date == today:
        return {"account_id": aid, "action": "done", "note": "already fired today"}, False
    if respect_times:
        if hhmm < entry_time:
            return {"account_id": aid, "action": "scheduled",
                    "note": f"signal fires {entry_time} ET"}, False
        grace = max(0, int(getattr(settings, "entry_grace_min", 10)))
        if grace and hhmm > _plus_minutes(entry_time, grace):
            return {"account_id": aid, "action": "missed",
                    "note": f"signal window {entry_time}+{grace}m passed"}, False
    plan = signal_plan(plan_key, drive)
    if plan is None:
        return {"account_id": aid, "action": "hold",
                "note": "no drive signal (flat open)"}, False
    row = {"account_id": aid, "action": "signal", "side": _side(plan.direction),
           "contracts": plan.contracts, "entry_time": entry_time,
           "note": f"signal channel: {plan_key}"}
    if not really_execute:
        return row, False
    if settings.hedge_guard:
        ok, reason = position_guard(broker, aid, nq)
        if not ok:
            # One attempt/day, same as strategy fires: a blocked signal fire
            # consumes the channel's day (followers get no late entry either).
            row["skipped"] = reason
            row["note"] = "attempt consumed - no retry today"
            st.last_fire_date = today
            merge_save(states, [aid])
            _trade.info("SKIP signal acct=%s %s — hedge guard: %s (attempt consumed)",
                        aid, plan_key, reason)
            return row, False
    entry_bal = balance.get(aid, st.equity)
    _trade.info("PLACE signal acct=%s %s %s x%d target=%.2fpt stop=%.2fpt drive=%s",
                aid, plan_key, _side(plan.direction), plan.contracts,
                plan.target_pts, plan.stop_pts, _side(drive))
    try:
        if not settings.hedge_guard and _open_size(broker, aid, nq):
            broker.close_contract(aid, nq)
        order_id = broker.place_bracket(aid, nq, plan)
        row["order_id"] = order_id
        record_pending(st, plan, entry_bal, today, settings.point_value)
        merge_save(states, [aid])
        _trade.info("PLACED signal acct=%s %s order_id=%s", aid, plan_key, order_id)
        _broker_order_state(broker, aid)
        return row, True
    except Exception as exc:
        row["error"] = str(exc)
        _trade.exception("SIGNAL FIRE FAILED acct=%s %s", aid, plan_key)
        # One attempt/day — but a timeout can raise after acceptance; a live
        # position means the signal actually fired and must reconcile.
        if _open_size(broker, aid, nq):
            record_pending(st, plan, entry_bal, today, settings.point_value)
            row["note"] = "place raised but position is open - treated as filled"
        else:
            st.last_fire_date = today
            row["note"] = "attempt consumed - no retry today"
        merge_save(states, [aid])
        return row, False


# Topstep rejects API bracket orders while the account's platform setting is
# "Position Brackets" instead of "Auto OCO Brackets" (per-account, web-UI only —
# no API reads or sets it): '/api/Order/place: error 2: Brackets cannot be used
# with Position Brackets. You must enable Auto OCO Brackets.' (live 2026-07-09,
# three fresh evals). The rejection text is the only detector we have; the
# nightly probe (tophat/services/oco_probe.py) uses the same rejection to check
# every account BEFORE the next morning's fire.
_OCO_ERR_SNIPPET = "auto oco"


def _alert_fire_failures(results: list[dict], names: dict[int, str],
                         settings, owner: str, today: str) -> None:
    """Discord-alert every fire attempt this pass that failed or was skipped.

    Attempts are one-per-account-per-day (a failure consumes the day), so each
    account can appear here at most once per day — no dedup needed. OCO
    rejections additionally stamp oco_blocked_on so the ops history records
    which setting was wrong. Best-effort: a bookkeeping failure must never
    abort a live session pass."""
    try:
        placed = [r["account_id"] for r in results if r.get("order_id")]
        blocked = [r["account_id"] for r in results
                   if _OCO_ERR_SNIPPET in str(r.get("error", "")).lower()]
        failed = [r for r in results
                  if not r.get("order_id") and (r.get("error") or r.get("skipped"))]
        if placed or blocked:
            with _REG_LOCK:
                reg = load_registry()
                for aid in placed:
                    reg.entry(aid).oco_blocked_on = ""
                for aid in blocked:
                    reg.entry(aid).oco_blocked_on = today
                save_registry(reg)
            invalidate_snapshot_cache()
        if not failed or not settings.discord_webhook_url:
            return
        from tophat.services.notify import post_discord
        blocked_set = set(blocked)

        def _why(r: dict) -> str:
            if r["account_id"] in blocked_set:
                return "rejected — Auto OCO Brackets is OFF"
            return str(r.get("error") or r.get("skipped"))[:140]
        lines = "\n".join(f"- **{names.get(r['account_id'], r['account_id'])}** "
                          f"(#{r['account_id']}): {_why(r)}" for r in failed)
        extra = ("\nEnable **Auto OCO Brackets** in the Topstep platform settings "
                 "before tomorrow — the nightly probe will re-check."
                 if blocked_set else "")
        post_discord(
            settings.discord_webhook_url,
            "🚨 Fire attempt failed — account is done for today",
            description=(f"{owner}: one attempt per account per day; these did "
                         f"NOT trade and will not retry:\n{lines}{extra}"),
            color=0xE74C3C)
    except Exception as exc:
        _trade.warning("fire-failure alerting failed: %s", exc)


def run_session(broker, *, execute: bool, respect_times: bool = False,
                now_et: datetime | None = None, manual: bool = False,
                owner: str = "") -> dict:
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
    now = now_et or datetime.now(ET)
    drive, drive_src = _drive(broker, nq, now)
    # Trading day (rolls ~4 PM CT). Morning entries fire well before the rollover
    # so this equals the ET calendar date whenever anything actually fires; it
    # only diverges in the evening, where automation never ticks (see _in_window).
    today = trading_day(now)
    hhmm = now.strftime("%H:%M")
    really_execute = execute and (manual or settings.auto_execute)

    accounts = broker.list_accounts()
    balance = {a.account_id: a.balance for a in accounts}
    phase_fixed = False

    tradeable = {}
    signal_keys: dict[int, str] = {}   # aid -> engine.SIGNAL_PLAN_TEMPLATES key
    practice_ids: set[int] = set()     # manual validation fires - never slot-assigned
    for a in accounts:
        entry = registry.entry(a.account_id)
        if not _effective_can_trade(a.can_trade, entry) or not entry.enabled:
            continue
        sig = entry.signal_plan if entry.signal_plan in SIGNAL_PLAN_TEMPLATES else ""
        if is_practice(a.name) and not sig:
            # Practice accounts never auto-trade or hold an eval slot. Two
            # exceptions: an explicit signal-channel designation (fires its signal
            # bracket for the copier), and manual Execute — the validation path,
            # which fires OUTSIDE assign_day so it can never take a capped
            # eval/nuke slot or write schedule rotation state.
            if not manual:
                continue
            practice_ids.add(a.account_id)
        is_new = a.account_id not in states
        st = get_or_create(states, a.account_id)
        if is_new:
            _init_state(st, cfg, a)
            phase_fixed = True
        elif sync_phase_from_name(st, a.name, cfg):
            phase_fixed = True
        if not is_practice(a.name) and _ratchet_peak_eod(
                cfg, st, a.balance, today, a.account_id):
            phase_fixed = True   # floor moved - persist via the end-of-pass merge
        tradeable[a.account_id] = st
        if sig:
            signal_keys[a.account_id] = sig

    # 1. Reconcile closed trades -> advance the payout cycle (live only).
    # Covers EVERY account this broker lists with a pending trade, not just the
    # tradeable set: Topstep flips canTrade=false the moment it liquidates an
    # account, which used to drop it from this loop BEFORE the blown trade was
    # booked — phase stayed 'funded', the loss never reached the trade log or
    # the mirrors, and the freed nuke slot rotated same-day (2026-07-09,
    # acct 25157729).
    reconciled = []
    reconciled_ids: list[int] = []
    auto_disabled: list[int] = []
    mirrors = load_mirrors() if really_execute else {}
    mirror_touched: list[str] = []
    names = {a.account_id: a.name for a in accounts}
    if really_execute:
        for a in accounts:
            aid = a.account_id
            st = states.get(aid)
            if st is None or not st.pending_label:
                continue
            try:
                flat = sum(int(p.get("size", 0))
                           for p in broker.search_open_positions(aid)) == 0
            except Exception as exc:
                # Never guess an outcome off a failed read — retry next tick.
                _trade.warning("position read failed acct=%s during reconcile: %s", aid, exc)
                continue
            # [debuglog] capture pre-reconcile fields (reconcile clears them on close)
            pend_label, entry_bal = st.pending_label, st.pending_entry_balance
            pend_date = st.pending_date or st.last_fire_date
            tgt_d, stp_d = st.pending_target_dollars, st.pending_stop_dollars
            cur_bal = balance.get(aid, st.equity)
            if not flat:
                _trade.info("STILL OPEN acct=%s %s entry_bal=$%.2f cur_bal=$%.2f (position not flat)",
                            aid, pend_label, entry_bal, cur_bal)
            out = reconcile(cfg, st, cur_bal, flat)
            if out:
                reconciled_ids.append(aid)
                reconciled.append({"account_id": aid, "trade": st.last_fire_date, "outcome": out})
                trig = {"win": "TARGET hit", "loss": "STOP/liquidation hit",
                        "flat": "closed ~breakeven (EOD/partial)"}.get(out, out)
                _trade.info("CLOSE acct=%s %s -> %s | entry_bal=$%.2f close_bal=$%.2f delta=$%+.2f "
                            "(target~$%.0f stop~$%.0f) | phase=%s days=%s win_days=%s nuke_hit=%s payout_ready=%s",
                            aid, pend_label, trig, entry_bal, cur_bal, cur_bal - entry_bal,
                            tgt_d, stp_d, st.phase.value, st.days_traded,
                            st.winning_days_this_cycle, st.nuke_hit_this_cycle, st.payout_ready)
                if st.phase == Phase.BLOWN:
                    _trade.warning("BLOWN acct=%s — trailing floor breached at bal=$%.2f", aid, cur_bal)
                # Feed the Analytics page: one immutable record per resolved trade.
                # Practice validation fires stay out - fake money isn't strategy P&L.
                # (Name check too: a practice pending can now reconcile on an auto
                # tick, where practice_ids is empty.)
                if aid not in practice_ids and not is_practice(names.get(aid, "")):
                    # account_name rides along so Analytics can still label the
                    # row after Topstep deletes the account (ids don't resolve
                    # against a pruned snapshot)
                    trade_log.log_event(
                        "trade", owner=owner, account_id=aid, trade_date=pend_date,
                        label=pend_label, outcome=out, pnl=round(cur_bal - entry_bal, 2),
                        balance=round(cur_bal, 2), phase=st.phase.value,
                        account_name=names.get(aid, ""))
                # Followers book the leader's ACTUAL delta scaled by their multiplier.
                mirror_touched.extend(mirror_sync.apply_leader_outcome(
                    mirrors, aid, cur_bal - entry_bal, today))
            if st.payout_ready and settings.auto_disable_on_payout_ready and registry.is_enabled(aid):
                registry.entry(aid).enabled = False  # surface for manual withdrawal
                auto_disabled.append(aid)

    # Signal channels, practice accounts and MLL-dead accounts never compete for
    # eval/nuke slots — the scheduler only ever sees the live strategy fleet.
    # (Dead accounts used to win slots and idle at the fire loop's floor check,
    # churning the day's slot stamps every tick and starving live accounts.)
    strategy = {aid: st for aid, st in tradeable.items()
                if aid not in signal_keys and aid not in practice_ids
                and not _below_mll(cfg, st, balance.get(aid, st.equity))}
    enabled_at = {aid: registry.entry(aid).enabled_at for aid in strategy}
    assignments, sched = assign_day(strategy, settings, today, load_schedule(),
                                    enabled_at, owner=owner)

    # 2. Fire due accounts.
    results = []
    placed = 0
    for aid, st in tradeable.items():
        if aid in signal_keys:
            row, did_place = _fire_signal(
                broker, aid, st, signal_keys[aid], drive, settings, states, balance,
                nq, today, hhmm, respect_times, really_execute)
            if did_place:
                placed += 1
            results.append(row)
            continue
        # Safety net: a balance at/below the MLL floor means the firm already
        # considers this account dead, whatever the API's canTrade says. Never
        # fire — and make it official: MLL is the source of truth for blown.
        bal = balance.get(aid)
        if bal is not None and _below_mll(cfg, st, bal):
            if _auto_mark_blown(cfg, st, bal, aid):
                merge_save(states, [aid])
            results.append({"account_id": aid, "action": "idle",
                            "note": "balance at/below MLL floor - blown"})
            continue
        if aid in practice_ids:
            # Manual validation fire: runs the engine's plan on the practice
            # account without holding a slot or touching the schedule.
            asg = Assignment(aid, "practice", "", "practice validation - no slot held")
        else:
            asg = assignments[aid]
        if asg.action in ("idle", "retire"):
            results.append({"account_id": aid, "action": asg.action, "note": asg.note})
            continue
        if st.payout_ready:
            results.append({"account_id": aid, "action": "payout_ready",
                            "note": "awaiting manual withdrawal"})
            continue
        if st.last_fire_date == today:
            # Also set by a failed/skipped attempt: one attempt per day, period.
            results.append({"account_id": aid, "action": "done",
                            "note": "already attempted today"})
            continue
        if respect_times and asg.entry_time:
            if hhmm < asg.entry_time:
                results.append({"account_id": aid, "action": "scheduled",
                                "note": f"fires {asg.entry_time} ET"})
                continue
            grace = max(0, int(getattr(settings, "entry_grace_min", 10)))
            if grace and hhmm > _plus_minutes(asg.entry_time, grace):
                # Too late to enter on-strategy (the drive edge is measured AT the
                # entry time) — e.g. a slot freed mid-morning by a passed eval, or
                # an account enabled after its stagger slot. Skip until tomorrow.
                results.append({"account_id": aid, "action": "missed",
                                "note": f"entry window {asg.entry_time}"
                                        f"+{grace}m passed - waiting for tomorrow"})
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
                # One attempt per account per day (operator rule, 2026-07-09):
                # the fire moment came and the account couldn't take it cleanly.
                # Retrying later in the window would enter minutes after the
                # drive read — off-strategy. The day is consumed.
                row["skipped"] = reason
                row["note"] = "attempt consumed - no retry today"
                st.last_fire_date = today
                merge_save(states, [aid])
                _trade.info("SKIP acct=%s %s — hedge guard: %s (attempt consumed)",
                            aid, dec.plan.label, reason)
            else:
                plan = dec.plan
                entry_bal = balance.get(aid, st.equity)
                if should_omit_stop(cfg, st, plan, entry_bal):
                    plan = replace(plan, manual_stop=False)
                    row["stop"] = "none (near floor - Topstep auto-liquidation)"
                tgt_d = plan.target_pts * plan.contracts * cfg.point_value
                stp_d = plan.stop_pts * plan.contracts * cfg.point_value
                # [debuglog] log the INTENT before the call, so a failed/timed-out place
                # is still visible (vs. only logging after we get an order_id back).
                _trade.info("PLACE acct=%s %s %s x%d target=%.2fpt(~$%.0f) stop=%s entry_bal=$%.2f drive=%s",
                            aid, plan.label, _side(plan.direction), plan.contracts,
                            plan.target_pts, tgt_d,
                            "NONE(auto-liq, near floor)" if not plan.manual_stop
                            else f"{plan.stop_pts:.2f}pt(~${stp_d:.0f})",
                            entry_bal, _side(drive))
                try:
                    # hedge_guard (when on) already proved this account flat, so there's
                    # nothing to close — and ProjectX errors ("error 2") when you close a
                    # flat account, which previously aborted the WHOLE session. Flatten
                    # only when hedge_guard is off and a position actually exists. And
                    # contain per-account failures so one bad account doesn't stop the fleet.
                    if not settings.hedge_guard and _open_size(broker, aid, nq):
                        broker.close_contract(aid, nq)
                    order_id = broker.place_bracket(aid, nq, plan)
                    row["order_id"] = order_id
                    record_pending(st, plan, entry_bal, today, cfg.point_value)
                    # Persist the fire IMMEDIATELY: if this session dies before its
                    # end-of-pass save, a lost last_fire_date means the next tick
                    # would re-fire this account.
                    merge_save(states, [aid])
                    placed += 1
                    _trade.info("PLACED acct=%s %s order_id=%s", aid, plan.label, order_id)
                    _broker_order_state(broker, aid)   # actual fill price + bracket prices
                except Exception as exc:
                    row["error"] = str(exc)
                    _trade.exception("FIRE FAILED acct=%s %s — fleet continues", aid, plan.label)
                    # One attempt per day: a failed place consumes the account's
                    # day (no ~30s retries — a later entry is off-strategy). But a
                    # timeout can raise AFTER the broker accepted the order, so
                    # first check for a live position; if one exists the fire
                    # actually happened and must reconcile like any other trade.
                    if _open_size(broker, aid, nq):
                        record_pending(st, plan, entry_bal, today, cfg.point_value)
                        row["note"] = "place raised but position is open - treated as filled"
                        _trade.warning("acct=%s place raised but a position exists — "
                                       "recorded as pending", aid)
                    else:
                        st.last_fire_date = today
                        row["note"] = "attempt consumed - no retry today"
                    merge_save(states, [aid])
        results.append(row)

    if really_execute:
        _alert_fire_failures(results, names, settings, owner, today)
        save_schedule(sched)
        # Merge-save only the accounts this pass touched, so we can't clobber a
        # concurrent writer's entries (snapshot inits, lifecycle edits).
        # Reconciled ids included: a liquidated account is no longer tradeable
        # but its BLOWN phase + cleared pending must persist.
        merge_save(states, list(set(tradeable) | set(reconciled_ids)))
        if mirror_touched:
            merge_save_mirrors(mirrors, mirror_touched)
        if auto_disabled:
            with _REG_LOCK:
                fresh = load_registry()
                for aid in auto_disabled:
                    fresh.entry(aid).enabled = False
                save_registry(fresh)
        invalidate_broker_cache()  # positions/balances changed - re-read the broker
        _trade.info("SESSION done — drive=%s(%s) tradeable=%d placed=%d reconciled=%d at %s ET",  # [debuglog]
                    _side(drive), drive_src, len(tradeable), placed, len(reconciled), hhmm)
    elif phase_fixed:
        merge_save(states, list(tradeable.keys()))
    return {"executed": really_execute, "drive": _side(drive), "drive_source": drive_src,
            "orders_placed": placed, "reconciled": reconciled, "results": results}


def ops_recap() -> dict:
    """Morning-digest payload: armed state, today's locked drive, reconciled
    outcomes, still-open trades and payout-ready accounts — assembled from
    THIS tenant's stores. Feeds the per-user daily Discord recap
    (services/daily_notify.py) and the session-authed /api/ops/recap."""
    from tophat.store.paths import ACCOUNT_NAMES_FILE
    settings = load_settings()
    today = trading_day(datetime.now(ET))
    names: dict[int, str] = {}
    try:
        raw = json.loads(tenant.resolve(ACCOUNT_NAMES_FILE).read_text(encoding="utf-8"))
        names.update({int(k): v for k, v in raw.items() if v})
    except Exception:
        pass
    registry = load_registry()
    for aid, e in registry.accounts.items():
        if e.alias:
            names[aid] = e.alias

    def label(aid: int) -> str:
        return names.get(aid) or f"#{aid}"

    results = []
    for e in trade_log.read_events():
        if e.get("type") != "trade" or e.get("account_id") is None:
            continue                     # mirror rows carry mirror_id, not account_id
        if (e.get("trade_date") or e.get("date")) != today:
            continue
        results.append({"account": label(int(e["account_id"])),
                        "label": e.get("label", "?"),
                        "outcome": e.get("outcome") or "flat",
                        "pnl": float(e.get("pnl") or 0.0)})
    still_open: list[str] = []
    ready: list[str] = []
    for aid, st in load_all().items():
        if st.phase in (Phase.PASSED, Phase.BLOWN, Phase.RETIRED):
            continue
        if st.pending_label and st.pending_date == today:
            still_open.append(f"{label(aid)} ({st.pending_label})")
        if st.payout_ready:
            ready.append(label(aid))
    for mid, m in load_mirrors().items():
        if m.payout_ready:
            ready.append(m.alias or mid)
    return {"ok": True, "armed": bool(settings.auto_execute),
            "trading_day": today, **drive_public(),
            "results": results, "still_open": still_open, "payout_ready": ready}


def mark_payout(account_id: int) -> dict:
    """Operator confirms a withdrawal; advance the account to its next payout cycle."""
    settings = load_settings()
    cfg = settings.to_account_config()
    states = load_all()
    st = states.get(account_id)
    if not st:
        return {"account_id": account_id, "error": "unknown account"}
    mark_payout_taken(cfg, st)
    merge_save(states, [account_id])
    # Withdrawal amount is manual at Topstep; record the rule's estimate
    # (min(cap, half the balance)) so Analytics can total banked payouts.
    trade_log.log_event(
        "payout", account_id=account_id, source="leader", estimated=True,
        amount=round(min(cfg.payout_cap, max(st.equity / 2.0, 0.0)), 2),
        payout_number=st.payouts_taken)
    # re-enable so it resumes trading the next cycle (unless retired)
    if st.phase.value != "retired":
        with _REG_LOCK:
            reg = load_registry()
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


def _reverse_recent_payout_events(n: int, **match) -> int:
    """Append negation events for the `n` most recent un-reversed payout events
    matching `match` (account_id=…/mirror_id=… plus source). Keeps Analytics'
    banked total truthful after a downward payouts_taken correction without
    rewriting the append-only ledger. Returns how many events were reversed."""
    events = [e for e in trade_log.read_events() if e.get("type") == "payout"
              and all(e.get(k) == v for k, v in match.items())]
    pos = [e for e in events if float(e.get("amount") or 0.0) > 0]
    already = sum(1 for e in events if float(e.get("amount") or 0.0) < 0)
    avail = pos[:-already] if already else pos
    take = avail[-n:] if n > 0 else []
    for src in reversed(take):
        trade_log.log_event(
            "payout", **match, estimated=bool(src.get("estimated")),
            via="override-correction",
            amount=-float(src.get("amount") or 0.0),
            payout_number=src.get("payout_number"))
    return len(take)


def list_accounts_detail(pool: list[BrokerHandle]) -> list[dict]:
    """Full account rows for the edit-accounts page, unioned across every credential."""
    # Build snapshots first — that's what creates+persists state for new accounts —
    # then read states so freshly discovered accounts are present.
    snaps = [(h, build_snapshot(h.broker, mode=h.mode, key=_ck(h.owner), owner=h.owner))
             for h in pool if h.broker is not None]
    states = load_all()
    registry = load_registry()
    out = []
    for h, snap in snaps:
        # Reuse the (cached) snapshot rows — they already carry every broker field plus
        # owner/lifecycle. Re-calling broker.list_accounts() here was a second, UNCACHED
        # /api/Account/search per credential on every Accounts-page load (429 risk).
        for row in snap["accounts"]:
            aid = row["account_id"]
            st = states.get(aid)
            if st is None:
                continue
            entry = registry.entry(aid)
            out.append({
                **row,
                "alias": entry.alias,
                "notes": entry.notes,
                "state": state_to_dict(st),
            })
    out.sort(key=lambda r: (r["owner"], r["phase"] != "funded", r["name"]))
    return out


def update_account_lifecycle(account_id: int, patch: dict, pool: list[BrokerHandle]) -> dict:
    """Patch lifecycle state and registry fields for one account."""
    handle = find_handle_for_account(pool, account_id)
    if handle is None:
        return {"account_id": account_id, "error": "unknown account"}
    # Cached broker reads - saving an account must not cost a REST round-trip.
    reads = _read_broker(handle.broker, _ck(handle.owner or handle.mode))
    accounts = {a.account_id: a for a in reads["accounts"]}
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
        if key not in patch or key == "payouts_taken":
            continue   # payouts_taken is applied LAST (below) with real semantics
        val = patch[key]
        if key == "phase":
            st.phase = Phase(str(val))
        elif key in ("nuke_hit_this_cycle", "payout_ready", "locked_out_today"):
            setattr(st, key, bool(val))
        elif key in ("days_traded", "winning_days_this_cycle",
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
    if "exclude_analytics" in patch:
        entry.exclude_analytics = bool(patch["exclude_analytics"])
    if "signal_plan" in patch:
        key = str(patch["signal_plan"] or "")
        if key and key not in SIGNAL_PLAN_TEMPLATES:
            return {"account_id": account_id,
                    "error": f"unknown signal plan {key!r} "
                             f"(known: {sorted(SIGNAL_PLAN_TEMPLATES)})"}
        entry.signal_plan = key

    if "sync_balance" in patch and patch["sync_balance"]:
        bal = accounts[account_id].balance
        st.equity = bal
        st.peak_equity_eod = max(st.peak_equity_eod, bal)

    # payouts_taken goes last so its side effects win over the (stale) cycle
    # fields the edit form always posts alongside it.
    payouts_recorded = payouts_reversed = 0
    if "payouts_taken" in patch:
        target = int(patch["payouts_taken"])
        if target > st.payouts_taken and patch.get("record_payouts"):
            # Operator recorded real withdrawals through the edit form: full
            # payout semantics per increment — identical to mark_payout(), so
            # Analytics, cycle resets, retirement and re-enable all follow.
            while st.payouts_taken < target:
                mark_payout_taken(cfg, st)
                trade_log.log_event(
                    "payout", account_id=account_id, source="leader",
                    estimated=True, via="override",
                    amount=round(min(cfg.payout_cap, max(st.equity / 2.0, 0.0)), 2),
                    payout_number=st.payouts_taken)
                payouts_recorded += 1
            if st.phase != Phase.RETIRED and not entry.enabled:
                entry.enabled = True        # resumes next cycle, same as mark_payout()
                entry.enabled_at = time.time()
        else:
            # Adoption/preset path (raise without record_payouts) or a downward
            # correction: set the counter verbatim. A decrement also reverses
            # the most recent logged payout event(s) so Analytics stays truthful.
            if target < st.payouts_taken:
                payouts_reversed = _reverse_recent_payout_events(
                    st.payouts_taken - target, account_id=account_id, source="leader")
            st.payouts_taken = target

    merge_save(states, [account_id])
    # Re-apply this account's (patched) registry entry over a fresh load, so we
    # only write the one entry we own and can't clobber concurrent edits.
    with _REG_LOCK:
        fresh_reg = load_registry()
        fresh_reg.accounts[account_id] = entry
        save_registry(fresh_reg)
    invalidate_snapshot_cache()
    a = accounts[account_id]
    eff = _effective_can_trade(a.can_trade, entry)
    return {
        "account_id": account_id,
        "ok": True,
        "payouts_recorded": payouts_recorded,
        "payouts_reversed": payouts_reversed,
        "lifecycle": ("practice" if is_practice(a.name)
                      else lifecycle_label(cfg, st, can_trade=eff)),
        "state": state_to_dict(st),
        "enabled": entry.enabled,
        "force_inactive": entry.force_inactive,
        "broker_can_trade": a.can_trade,
        "can_trade": eff,
        "alias": entry.alias,
        "exclude_analytics": entry.exclude_analytics,
        "signal_plan": entry.signal_plan,
    }
