"""Aggregate the trade log + fleet state into the Analytics page payload.

Live win rates come from reconciled trades in data/trade_log.jsonl (flat
outcomes are excluded from the rate, mirroring the backtest's treatment of
unresolved days). Ticket spend is an ESTIMATE from fleet counts x firm list
prices - purchases happen outside TopHat, so the UI labels it "est.".
"""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.services.status import is_practice
from tophat.store import trade_log
from tophat.store.firms import FIRMS, FOLLOWER_FIRMS, TOPSTEP
from tophat.store.mirrors import load_mirrors
from tophat.store.registry import load_registry
from tophat.store.states import load_all

# Which persisted states count toward fleet/funnel/spend: an account must be
# visible on a live credential right now OR have real recorded activity in the
# trade log. Anything else (mock leftovers, states from removed API keys) is
# ignored so Analytics reflects the actual fleet, not every file on disk.

ET = ZoneInfo("America/New_York")

# Backtested reference win rates per leg (docs/PROBABILITY.md; MULTI_FIRM_PLAN §3):
# label -> (coin-flip baseline, drive edge). The sig-* labels are the Apex-native
# signal-channel brackets.
MODEL_WR = {
    "eval":          (0.400, 0.466),
    "nuke":          (0.238, 0.279),
    "renuke":        (0.192, 0.236),
    "flip":          (0.855, 0.884),
    "sig-apex-nuke": (0.435, 0.488),
    # 14.25/50 flip + 31.5/10 eval, measured on the 285-day tick cache
    # (brackets re-locked 2026-07-06; docs/STATS_AUDIT_2026-07-06.md)
    "sig-apex-flip": (0.794, 0.844),
    "sig-apex-eval": (0.222, 0.295),
}
MODEL_EVAL_PASS = 0.424   # corrected intraday-MLL model (PROBABILITY.md §0)


def _leg_rows(trades: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for t in trades:
        label = str(t.get("label") or "?")
        row = by.setdefault(label, {"label": label, "n": 0, "wins": 0,
                                    "losses": 0, "flats": 0, "pnl": 0.0})
        row["n"] += 1
        out = t.get("outcome")
        if out == "win":
            row["wins"] += 1
        elif out == "loss":
            row["losses"] += 1
        else:
            row["flats"] += 1
        row["pnl"] += float(t.get("pnl") or 0.0)
    rows = []
    # keep the model's leg order first, then any unexpected labels
    for label in list(MODEL_WR) + sorted(set(by) - set(MODEL_WR)):
        row = by.get(label, {"label": label, "n": 0, "wins": 0,
                             "losses": 0, "flats": 0, "pnl": 0.0})
        decided = row["wins"] + row["losses"]
        wr = row["wins"] / decided if decided else None
        ci = (1.96 * math.sqrt(wr * (1 - wr) / decided)
              if decided and wr is not None else None)
        coin, drive = MODEL_WR.get(label, (None, None))
        rows.append({**row, "pnl": round(row["pnl"], 2), "live_wr": wr,
                     "ci95": ci, "model_coin": coin, "model_drive": drive})
    return rows


# Topstep legs that trade FUNDED money. Evals never chart: a ticket's paper
# P&L is a stepping stone, not profit (operator decision 2026-07-08).
FUNDED_LABELS = {"nuke", "renuke", "flip"}


def _curves(trades: list[dict], mirror_trades: list[dict],
            payouts: list[dict]) -> dict:
    """Per-day cumulative profit series for the chart: one per firm's funded
    accounts, their sum ("All funded" — the fleet's equity curve; payouts are
    day P&L already booked, so withdrawals don't dip it), and banked payouts
    (cash actually extracted). Leader funded legs are picked by label; mirror
    events carry their phase explicitly."""
    firm_short = {k: k.split("-")[0] for k in FOLLOWER_FIRMS}   # apex-50k -> apex
    daily: dict[str, dict[str, float]] = {}

    def add(day: str, key: str, v: float) -> None:
        if day:
            d = daily.setdefault(day, {})
            d[key] = d.get(key, 0.0) + v

    for t in trades:
        if str(t.get("label")) in FUNDED_LABELS:
            add(str(t.get("trade_date") or t.get("date") or ""), "topstep",
                float(t.get("pnl") or 0.0))
    for t in mirror_trades:
        key = firm_short.get(str(t.get("firm")))
        if key and t.get("phase") == "funded":
            add(str(t.get("trade_date") or t.get("date") or ""), key,
                float(t.get("pnl") or 0.0))
    for p in payouts:
        add(str(p.get("date") or ""), "banked", float(p.get("amount") or 0.0))

    firm_keys = ["topstep"] + [firm_short[k] for k in sorted(FOLLOWER_FIRMS)]
    labels = {"topstep": f"{TOPSTEP.label} funded",
              **{firm_short[k]: f"{p.label} funded"
                 for k, p in FOLLOWER_FIRMS.items()}}
    dates = sorted(daily)
    run = dict.fromkeys([*firm_keys, "banked"], 0.0)
    cum: dict[str, list[float]] = {k: [] for k in [*firm_keys, "total", "banked"]}
    total = 0.0
    for d in dates:
        for k in [*firm_keys, "banked"]:
            run[k] += daily[d].get(k, 0.0)
            cum[k].append(round(run[k], 2))
        total += sum(daily[d].get(k, 0.0) for k in firm_keys)
        cum["total"].append(round(total, 2))
    if dates:
        # Cumulative profit was $0 the day before the first chartable day -
        # prepend that baseline so the chart draws a line from day one instead
        # of sitting blank until a second day lands.
        from datetime import date as _date, timedelta as _td
        try:
            day0 = (_date.fromisoformat(dates[0]) - _td(days=1)).isoformat()
            dates = [day0, *dates]
            for k in cum:
                cum[k].insert(0, 0.0)
        except ValueError:
            pass   # unparseable first date: fall back to the raw series
    series = ([{"key": k, "label": labels[k], "cum": cum[k]} for k in firm_keys]
              + [{"key": "total", "label": "All funded", "cum": cum["total"]},
                 {"key": "banked", "label": "Payouts banked", "cum": cum["banked"]}])
    for s in series:
        s["has_data"] = any(abs(v) > 1e-9 for v in s["cum"])
    return {"dates": dates, "series": series}


def _fleet_and_spend(pool, log_ids: set, excluded: set) -> tuple[dict, dict, list[dict]]:
    """Fleet snapshot + estimated ticket spend from account counts x list prices.

    `log_ids` = account ids with trade-log activity (kept even after Topstep
    deletes a passed eval); `excluded` = operator-excluded ids (Accounts page)."""
    states = load_all()
    registry = load_registry()
    names: dict[int, str] = {}
    balances: dict[int, float] = {}
    try:
        from tophat.server import service
        names, balances = service.account_names_and_balances(pool)
    except Exception:
        pass
    # Registry accounts whose recorded phase is TERMINAL count as fleet too: an
    # eval that passes (or blows) on a day with no reconciled trade vanishes
    # from the broker without ever reaching the trade log, and its ticket would
    # silently drop out of the spend/funnel. Terminal-only keeps stale
    # non-terminal leftovers (old practice/mock states) from resurfacing —
    # anything still active is visible via `names` anyway.
    terminal = {aid for aid in registry.accounts
                if states.get(aid) is not None
                and states[aid].phase.value in ("passed", "blown", "retired")}
    tracked = set(names) | set(log_ids) | terminal

    leaders = {"eval": 0, "funded": 0, "passed": 0, "blown_eval": 0,
               "blown_funded": 0, "retired": 0}
    n_eval_accounts = n_funded_accounts = 0
    funded_equity = eval_progress = 0.0
    for aid, st in states.items():
        if aid not in tracked or aid in excluded:
            continue   # stale/mock state or operator-excluded - not part of the fleet
        name = names.get(aid, "")
        if (name and is_practice(name)) or registry.entry(aid).signal_plan:
            continue   # not a purchased strategy ticket
        is_eval_origin = st.base_balance >= 25_000
        phase = st.phase.value
        # live broker balance beats the tracked equity when the account is visible
        equity = balances.get(aid, st.equity)
        if is_eval_origin:
            n_eval_accounts += 1
            if phase == "eval":
                leaders["eval"] += 1
                eval_progress += equity - st.base_balance
            elif phase == "passed":
                leaders["passed"] += 1
            elif phase == "blown":
                leaders["blown_eval"] += 1
        else:
            n_funded_accounts += 1
            if phase == "funded":
                leaders["funded"] += 1
                funded_equity += equity
            elif phase == "blown":
                leaders["blown_funded"] += 1
            elif phase == "retired":
                leaders["retired"] += 1

    spend_rows = []
    if n_eval_accounts:
        spend_rows.append({"what": f"{TOPSTEP.label} evals", "count": n_eval_accounts,
                           "unit": TOPSTEP.ticket_cost,
                           "cost": n_eval_accounts * TOPSTEP.ticket_cost})
    if n_funded_accounts and TOPSTEP.activation_cost:
        spend_rows.append({"what": f"{TOPSTEP.label} activations",
                           "count": n_funded_accounts, "unit": TOPSTEP.activation_cost,
                           "cost": n_funded_accounts * TOPSTEP.activation_cost})

    mirrors = load_mirrors()
    mirrors_by_phase: dict[str, int] = {}
    mirror_equity = 0.0
    per_firm: dict[str, dict[str, int]] = {}
    for m in mirrors.values():
        mirrors_by_phase[m.phase] = mirrors_by_phase.get(m.phase, 0) + 1
        f = per_firm.setdefault(m.firm, {"tickets": 0, "activations": 0})
        f["tickets"] += 1
        if m.phase in ("funded", "waiting", "retired") or m.payouts_taken > 0:
            f["activations"] += 1
        if m.phase == "funded":
            mirror_equity += m.equity
    for firm_key, c in sorted(per_firm.items()):
        prof = FIRMS.get(firm_key)
        if prof is None:
            continue
        spend_rows.append({"what": f"{prof.label} evals", "count": c["tickets"],
                           "unit": prof.ticket_cost,
                           "cost": c["tickets"] * prof.ticket_cost})
        if c["activations"] and prof.activation_cost:
            spend_rows.append({"what": f"{prof.label} activations",
                               "count": c["activations"], "unit": prof.activation_cost,
                               "cost": c["activations"] * prof.activation_cost})

    fleet = {
        "leaders": leaders,
        "mirrors_by_phase": mirrors_by_phase,
        "mirrors_total": len(mirrors),
        "funded_equity": round(funded_equity, 2),
        "eval_progress": round(eval_progress, 2),
        "mirror_equity": round(mirror_equity, 2),
    }
    resolved = leaders["passed"] + leaders["blown_eval"]
    funnel = {
        "started": n_eval_accounts,
        "active": leaders["eval"],
        "passed": leaders["passed"],
        "blown": leaders["blown_eval"],
        "pass_rate": leaders["passed"] / resolved if resolved else None,
        "model_pass": MODEL_EVAL_PASS,
        "funded_active": leaders["funded"],
        "funded_blown": leaders["blown_funded"],
        "retired": leaders["retired"],
    }
    return fleet, funnel, spend_rows


def build_analytics(pool) -> dict:
    events = trade_log.read_events()
    registry = load_registry()
    excluded = {aid for aid, e in registry.accounts.items() if e.exclude_analytics}
    trades_all = [e for e in events if e.get("type") == "trade"
                  and e.get("account_id") not in excluded]
    # Mirror day-bookings (source="mirror") feed ONLY the per-firm curves;
    # legs, realized P&L and the recent-trades table stay leader-only.
    trades = [e for e in trades_all if e.get("source") != "mirror"]
    mirror_trades = [e for e in trades_all if e.get("source") == "mirror"]
    payouts = [e for e in events if e.get("type") == "payout"
               and e.get("account_id") not in excluded]
    log_ids = {e.get("account_id") for e in events
               if e.get("account_id") is not None}

    fleet, funnel, spend_rows = _fleet_and_spend(pool, log_ids, excluded)
    spend_total = round(sum(r["cost"] for r in spend_rows), 2)
    banked = round(sum(float(p.get("amount") or 0.0) for p in payouts), 2)
    realized = round(sum(float(t.get("pnl") or 0.0) for t in trades), 2)

    names: dict[int, str] = {}
    try:
        from tophat.server import service
        names = service.account_names(pool)
    except Exception:
        pass
    # Deleted/rotated accounts (Topstep prunes passed evals and blown nukes)
    # drop out of the live snapshot, which used to leave "#25157729" rows.
    # Fall back to the name the event recorded at log time, then to the
    # write-through account_names.json cache.
    cached_names: dict[int, str] = {}
    try:
        import json as _json
        from tophat.store import tenant
        from tophat.store.paths import ACCOUNT_NAMES_FILE
        p = tenant.resolve(ACCOUNT_NAMES_FILE)
        if p.exists():
            cached_names = {int(k): str(v) for k, v in
                            _json.loads(p.read_text(encoding="utf-8")).items() if v}
    except Exception:
        pass

    reg_alias = {aid: e.alias for aid, e in registry.accounts.items()
                 if getattr(e, "alias", "")}

    def name_of(aid, event_name: str = "") -> str:
        return (names.get(aid) or event_name or cached_names.get(aid)
                or reg_alias.get(aid) or f"#{aid}")

    recent = []
    for t in reversed(trades[-15:]):
        aid = t.get("account_id")
        recent.append({
            "date": t.get("trade_date") or t.get("date") or "",
            "account": name_of(aid, str(t.get("account_name") or "")),
            "owner": t.get("owner") or "",
            "label": t.get("label") or "?",
            "outcome": t.get("outcome") or "?",
            "pnl": float(t.get("pnl") or 0.0),
            "balance": t.get("balance"),
        })
    recent_payouts = []
    for p in reversed(payouts[-10:]):
        who = (name_of(p.get("account_id"))
               if p.get("source") == "leader" else str(p.get("mirror_id") or "?"))
        recent_payouts.append({"date": p.get("date") or "", "who": who,
                               "source": p.get("source") or "leader",
                               "amount": float(p.get("amount") or 0.0),
                               "estimated": bool(p.get("estimated"))})

    return {
        "as_of": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "has_data": bool(trades or payouts),
        "trades_recorded": len(trades),
        "totals": {
            "realized_pnl": realized,
            "payouts_banked": banked,
            "spend_est": spend_total,
            "net_cash_est": round(banked - spend_total, 2),
        },
        "legs": _leg_rows(trades),
        "curves": _curves(trades, mirror_trades, payouts),
        "funnel": funnel,
        "fleet": fleet,
        "spend_breakdown": spend_rows,
        "recent": recent,
        "recent_payouts": recent_payouts,
    }
