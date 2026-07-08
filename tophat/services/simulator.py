"""Simulation engine: backtest + Monte Carlo over the cached NQ bars
(1-second bars built from NinjaTrader tick data - see store/simdata.py).

`run_sim(params, views)` is a pure function - same params + day views + seed in,
same result out - so it is fully unit-testable (docs/SIMULATION_PLAN.md).

Fidelity contract: each simulated day resolves the bracket first-touch on the
real price path (same-bar ties default pessimistic: stop wins), and the
lifecycle advances through the SAME code the live system uses - engine.decide()
picks the day's bracket and lifecycle's classify/advance functions book it - so
the simulator can never quietly diverge from live behavior.

Money model: the fan chart and net distributions are NET CASH = payouts banked
minus tickets/activations spent. Account equity above base is paper (Topstep
resets it at pass/payout), so it never counts as cash.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields

import numpy as np

from tophat.engine import (
    AccountConfig, AccountState, Action, Phase, decide, is_dead, start_new_day)
# Deliberate private-function reuse: these ARE the live reconcile semantics
# (half-way win/loss thresholds, winning-day and payout-cycle advancement).
from tophat.services.lifecycle import (  # noqa: PLC2701
    _advance_eval, _advance_funded, _classify, mark_payout_taken)
from tophat.store.config import _norm_hhmm
from tophat.store.simdata import DayView

LIFECYCLES = ("full", "eval", "funded", "single")
DIRECTION_RULES = ("drive", "long", "short", "coin")
SAMPLING = ("bootstrap", "sequential")

MAX_PATHS = 20_000
MAX_DAYS = 1_000
MAX_TICKETS = 100
CURVE_HORIZON = 130          # fan-chart days (paths absorb their final value)
CURVE_BANDS = (10, 25, 50, 75, 90)


@dataclass
class SimParams:
    # lifecycle / setup
    lifecycle: str = "full"          # full | eval | funded | single
    direction_rule: str = "drive"    # drive | long | short | coin
    entry_time: str = "09:45"        # ET wall clock
    date_from: str = ""              # optional YYYY-MM-DD bounds on the day pool
    date_to: str = ""
    # account model (engine AccountConfig fields)
    initial_balance: float = 50_000.0
    funded_start: float = 0.0
    dll: float = 1_000.0
    trailing: float = 2_000.0
    point_value: float = 20.0
    # eval leg
    eval_contracts: int = 5
    eval_target_pts: float = 15.5
    eval_stop_pts: float = 10.0
    eval_target_dollars: float = 3_000.0
    eval_min_days: int = 2
    # funded legs
    funded_contracts: int = 2
    nuke_target_dollars: float = 3_200.0
    flip_contracts: int = 1
    flip_target_dollars: float = 170.0
    winning_days_required: int = 5
    payout_cap: float = 2_000.0
    payouts_target: int = 4
    # single-bracket mode
    single_contracts: int = 2
    single_target_pts: float = 32.5
    single_stop_pts: float = 25.0
    # costs (prefilled from the firm profile client-side, editable)
    ticket_cost: float = 85.0
    activation_cost: float = 0.0
    # run model
    n_paths: int = 2_000
    seed: int = 7
    sampling: str = "bootstrap"      # bootstrap | sequential (real day order)
    max_days: int = 250
    tickets: int = 1                 # fleet-of-independent-tickets multiplier
    # When target AND stop hit inside one bar the true order is unknown (rare
    # at 1-second resolution). "pessimistic" books the stop (research default);
    # "optimistic" books the target. Run both to bracket reality.
    tie_rule: str = "pessimistic"

    def validate(self) -> None:
        if self.lifecycle not in LIFECYCLES:
            raise ValueError(f"lifecycle must be one of {LIFECYCLES}")
        if self.direction_rule not in DIRECTION_RULES:
            raise ValueError(f"direction_rule must be one of {DIRECTION_RULES}")
        if self.sampling not in SAMPLING:
            raise ValueError(f"sampling must be one of {SAMPLING}")
        if self.tie_rule not in ("pessimistic", "optimistic"):
            raise ValueError("tie_rule must be pessimistic or optimistic")
        self.entry_time = _norm_hhmm(self.entry_time)
        if not ("09:31" <= self.entry_time <= "15:00"):
            raise ValueError("entry_time must be between 09:31 and 15:00 ET")
        self.n_paths = int(min(max(int(self.n_paths), 1), MAX_PATHS))
        self.max_days = int(min(max(int(self.max_days), 5), MAX_DAYS))
        self.tickets = int(min(max(int(self.tickets), 1), MAX_TICKETS))
        if self.n_paths * self.max_days > 5_000_000:
            raise ValueError("run too large: paths x max_days must be "
                             "5,000,000 or less")
        for name in ("point_value", "eval_target_pts", "eval_stop_pts",
                     "single_target_pts", "single_stop_pts", "nuke_target_dollars",
                     "flip_target_dollars", "eval_target_dollars", "dll", "trailing"):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("eval_contracts", "funded_contracts", "flip_contracts",
                     "single_contracts"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be at least 1")

    def to_config(self) -> AccountConfig:
        return AccountConfig(
            point_value=self.point_value,
            initial_balance=self.initial_balance,
            funded_initial_balance=self.funded_start,
            dll=self.dll,
            trailing_drawdown=self.trailing,
            eval_contracts=int(self.eval_contracts),
            eval_target_dollars=self.eval_target_dollars,
            eval_target_pts=self.eval_target_pts,
            eval_stop_pts=self.eval_stop_pts,
            eval_min_days=int(self.eval_min_days),
            funded_contracts=int(self.funded_contracts),
            flip_contracts=int(self.flip_contracts),
            nuke_target_dollars=self.nuke_target_dollars,
            flip_target_dollars=self.flip_target_dollars,
            payout_cap=self.payout_cap,
            winning_days_required=int(self.winning_days_required),
            payouts_target=int(self.payouts_target),
        )


def params_from_dict(raw: dict) -> SimParams:
    known = {f.name for f in fields(SimParams)}
    p = SimParams(**{k: v for k, v in (raw or {}).items() if k in known})
    p.validate()
    return p


# ------------------------------------------------------------ day resolution
def resolve_day(view: DayView, direction: int, tgt_pts: float, stp_pts: float,
                *, tie_pessimistic: bool = True) -> float:
    """Signed points realized for one day: first-touch on the cached bars.

    A tie (target AND stop inside one bar) can't be ordered from bar data:
    pessimistic books the stop (research default), optimistic the target.
    Unresolved days exit at the 16:00 close.
    """
    if direction == 1:
        hit_t = view.hi >= view.entry + tgt_pts
        hit_s = view.lo <= view.entry - stp_pts
    else:
        hit_t = view.lo <= view.entry - tgt_pts
        hit_s = view.hi >= view.entry + stp_pts
    it = int(np.argmax(hit_t)) if hit_t.any() else -1
    is_ = int(np.argmax(hit_s)) if hit_s.any() else -1
    if it == -1 and is_ == -1:
        return (view.eod_close - view.entry) * direction
    if is_ == -1:
        return tgt_pts
    if it == -1:
        return -stp_pts
    if it == is_:                   # tie bar
        return -stp_pts if tie_pessimistic else tgt_pts
    return tgt_pts if it < is_ else -stp_pts


class _OutcomeMemo:
    """Lazy cache of resolve_day results keyed (day, direction, bracket)."""

    def __init__(self, views: list[DayView], tie_pessimistic: bool = True):
        self.views = views
        self.tie_pessimistic = tie_pessimistic
        self._memo: dict[tuple, float] = {}

    def points(self, day_idx: int, direction: int,
               tgt_pts: float, stp_pts: float) -> float:
        key = (day_idx, direction, round(tgt_pts, 4), round(stp_pts, 4))
        got = self._memo.get(key)
        if got is None:
            got = resolve_day(self.views[day_idx], direction, tgt_pts, stp_pts,
                              tie_pessimistic=self.tie_pessimistic)
            self._memo[key] = got
        return got


# ------------------------------------------------------------ one MC path
def _fresh_state(cfg: AccountConfig, lifecycle: str) -> AccountState:
    if lifecycle == "funded":
        return AccountState(phase=Phase.FUNDED, equity=cfg.funded_initial_balance,
                            peak_equity_eod=cfg.funded_initial_balance,
                            base_balance=cfg.funded_initial_balance)
    return AccountState(phase=Phase.EVAL, equity=cfg.initial_balance,
                        peak_equity_eod=cfg.initial_balance,
                        base_balance=cfg.initial_balance)


def _activate_funded(cfg: AccountConfig) -> AccountState:
    return AccountState(phase=Phase.FUNDED, equity=cfg.funded_initial_balance,
                        peak_equity_eod=cfg.funded_initial_balance,
                        base_balance=cfg.funded_initial_balance)


def _day_direction(view: DayView, rule: str, rng: np.random.Generator) -> int:
    if rule == "drive":
        return view.direction
    if rule == "long":
        return 1
    if rule == "short":
        return -1
    return 1 if rng.random() < 0.5 else -1        # coin


def _run_path(rng: np.random.Generator, views: list[DayView], memo: _OutcomeMemo,
              cfg: AccountConfig, p: SimParams) -> dict:
    n_days_pool = len(views)
    st = _fresh_state(cfg, p.lifecycle)
    start_eq = st.equity
    banked = 0.0
    costs = p.ticket_cost + (p.activation_cost if p.lifecycle == "funded" else 0.0)
    passed = False
    day = 0
    curve = np.empty(CURVE_HORIZON)
    seq_pos = int(rng.integers(n_days_pool)) if p.sampling == "sequential" else 0

    def net_now() -> float:
        # Lifecycle modes: cash = payouts banked - costs (equity above base is
        # paper; the firm resets it). Single-bracket mode has no payout
        # mechanics at all - the day P&L stream IS the result, so mark equity
        # to market (otherwise every single-mode net reads -ticket_cost).
        if p.lifecycle == "single":
            return (st.equity - start_eq) - costs
        return banked - costs

    while day < p.max_days:
        if p.sampling == "sequential":
            idx = (seq_pos + day) % n_days_pool
        else:
            idx = int(rng.integers(n_days_pool))
        view = views[idx]
        direction = _day_direction(view, p.direction_rule, rng)

        if direction != 0:
            if p.lifecycle == "single":
                pts = memo.points(idx, direction, p.single_target_pts,
                                  p.single_stop_pts)
                st.equity += pts * p.single_contracts * cfg.point_value
                st.days_traded += 1
                st.peak_equity_eod = max(st.peak_equity_eod, st.equity)
                if is_dead(cfg, st):
                    st.phase = Phase.BLOWN
            else:
                start_new_day(st)
                dec = decide(cfg, st, direction)
                if dec.action == Action.TRADE and dec.plan is not None:
                    plan = dec.plan
                    pts = memo.points(idx, direction, plan.target_pts, plan.stop_pts)
                    delta = pts * plan.contracts * cfg.point_value
                    tgt_d = plan.target_pts * plan.contracts * cfg.point_value
                    stp_d = plan.stop_pts * plan.contracts * cfg.point_value
                    outcome = _classify(delta, tgt_d, stp_d)
                    new_balance = st.equity + delta
                    if st.phase == Phase.EVAL:
                        _advance_eval(cfg, st, outcome, new_balance)
                    else:
                        _advance_funded(cfg, st, plan.label, outcome, delta)
                    st.equity = new_balance
                    st.peak_equity_eod = max(st.peak_equity_eod, st.equity)
                    if (st.phase in (Phase.EVAL, Phase.FUNDED)
                            and is_dead(cfg, st)):
                        st.phase = Phase.BLOWN
                    if st.payout_ready:
                        amount = min(cfg.payout_cap, max(st.equity / 2.0, 0.0))
                        banked += amount
                        st.equity -= amount
                        mark_payout_taken(cfg, st)

        if day < CURVE_HORIZON:
            curve[day] = net_now()
        day += 1

        if st.phase == Phase.PASSED:
            passed = True
            if p.lifecycle == "full":
                costs += p.activation_cost
                st = _activate_funded(cfg)
            else:
                break
        if st.phase in (Phase.BLOWN, Phase.RETIRED):
            break

    if day < CURVE_HORIZON:
        curve[day:] = net_now()
    return {
        "net": net_now(),
        "banked": banked,
        "costs": costs,
        "payouts": st.payouts_taken,
        "passed": passed,
        "blown": st.phase == Phase.BLOWN,
        "retired": st.phase == Phase.RETIRED,
        "days": day,
        "terminal": st.phase in (Phase.BLOWN, Phase.RETIRED) or (
            passed and p.lifecycle == "eval"),
        "curve": curve,
    }


# ------------------------------------------------------------ leg summary
def _leg_summary(views: list[DayView], memo: _OutcomeMemo,
                 cfg: AccountConfig, p: SimParams) -> list[dict]:
    """Deterministic per-leg win rates over the drive-directed day pool."""
    from tophat.server.analytics import MODEL_WR
    if p.lifecycle == "single":
        legs = [("single", p.single_target_pts, p.single_stop_pts)]
    else:
        n0t, n0s = cfg.nuke_bracket_pts(0)
        n1t, n1s = cfg.nuke_bracket_pts(1)
        ft, fs = cfg.flip_bracket_pts()
        legs = [("eval", cfg.eval_target_pts, cfg.eval_stop_pts),
                ("nuke", n0t, n0s), ("renuke", n1t, n1s), ("flip", ft, fs)]
        if p.lifecycle == "funded":
            legs = legs[1:]
        elif p.lifecycle == "eval":
            legs = legs[:1]
    out = []
    for label, tgt, stp in legs:
        wins = losses = 0
        for i, v in enumerate(views):
            if v.direction == 0:
                continue
            pts = memo.points(i, v.direction, tgt, stp)
            if pts >= tgt - 1e-9:
                wins += 1
            elif pts <= -stp + 1e-9:
                losses += 1
        decided = wins + losses
        model = MODEL_WR.get(label, (None, None))
        out.append({
            "label": label,
            "target_pts": round(tgt, 2), "stop_pts": round(stp, 2),
            "n": decided,
            "wr": wins / decided if decided else None,
            "coin_floor": stp / (tgt + stp),
            "model_drive": model[1],
        })
    return out


# ------------------------------------------------------------ entry point
def run_sim(params: SimParams, views: list[DayView]) -> dict:
    """Pure Monte Carlo run. Deterministic for a given (params, views, seed)."""
    t0 = time.monotonic()
    if params.date_from:
        views = [v for v in views if v.date >= params.date_from]
    if params.date_to:
        views = [v for v in views if v.date <= params.date_to]
    if len(views) < 20:
        raise ValueError(f"only {len(views)} usable days in the selected range - "
                         "need at least 20")
    cfg = params.to_config()
    memo = _OutcomeMemo(views, tie_pessimistic=params.tie_rule == "pessimistic")
    rng = np.random.default_rng(int(params.seed))

    paths = [_run_path(rng, views, memo, cfg, params)
             for _ in range(params.n_paths)]

    nets = np.array([x["net"] for x in paths])
    days_arr = np.array([x["days"] for x in paths])
    curves = np.stack([x["curve"] for x in paths])
    n = float(len(paths))

    probs = {
        "eval_passed": sum(x["passed"] for x in paths) / n,
        "blown": sum(x["blown"] for x in paths) / n,
        "retired": sum(x["retired"] for x in paths) / n,
        "paid_once": sum(x["payouts"] >= 1 for x in paths) / n,
        "profitable": float((nets > 0).mean()),
    }
    success_key = {"eval": "eval_passed", "funded": "paid_once",
                   "full": "paid_once", "single": "profitable"}[params.lifecycle]
    success_label = {"eval": "eval pass rate", "funded": "reaches a payout",
                     "full": "reaches a payout",
                     "single": "profitable"}[params.lifecycle]

    fleet = None
    if params.tickets > 1:
        draws = rng.integers(len(nets), size=(params.n_paths, params.tickets))
        fleet_nets = nets[draws].sum(axis=1)
        fleet = {"tickets": params.tickets,
                 "mean": round(float(fleet_nets.mean()), 2),
                 "p5": round(float(np.percentile(fleet_nets, 5)), 2),
                 "p50": round(float(np.percentile(fleet_nets, 50)), 2),
                 "p95": round(float(np.percentile(fleet_nets, 95)), 2)}

    curve_out = {"days": list(range(1, CURVE_HORIZON + 1))}
    for b in CURVE_BANDS:
        curve_out[f"p{b}"] = [round(float(x), 2)
                              for x in np.percentile(curves, b, axis=0)]

    return {
        "ok": True,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "params": asdict(params),
        "days_used": len(views),
        "date_from": views[0].date,
        "date_to": views[-1].date,
        "success": {"label": success_label, "prob": round(probs[success_key], 4)},
        "probs": {k: round(v, 4) for k, v in probs.items()},
        "net": {
            "mean": round(float(nets.mean()), 2),
            "median": round(float(np.percentile(nets, 50)), 2),
            "p5": round(float(np.percentile(nets, 5)), 2),
            "p95": round(float(np.percentile(nets, 95)), 2),
            "mean_banked": round(float(np.mean([x["banked"] for x in paths])), 2),
            "mean_costs": round(float(np.mean([x["costs"] for x in paths])), 2),
            # average COUNT of payouts taken per ticket (mean_banked is dollars)
            "mean_payouts": round(float(np.mean([x["payouts"] for x in paths])), 2),
        },
        "days_to_outcome": {
            "mean": round(float(days_arr.mean()), 1),
            "median": float(np.percentile(days_arr, 50)),
        },
        "fleet": fleet,
        "legs": _leg_summary(views, memo, cfg, params),
        "curve": curve_out,
    }
