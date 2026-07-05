"""The nightly deterministic solver: who follows whom tomorrow, what to buy,
and the exact Tradecopia edit list (docs/MULTI_FIRM_PLAN.md §5).

`build_plan` is a pure function — same leaders + mirrors + settings in, same plan
out — so it's fully unit-testable and re-running it is always safe. The emitted
lines are a DIFF between each mirror's current mapping (leader_id / channel /
multiplier stored on the mirror) and the computed desired mapping; applying the
plan (`apply_plan`) persists the desired mapping into the mirror store, so an
applied plan re-solves to zero lines.

Rule blocks, in order (PLAN §5): PAIR (events -> desired mappings), SCHEDULE
(apex intake/nuke-slot rotation), REPLENISH (buy list), EMIT (diff + channels +
payout queue + hazards).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tophat.services import mirror_sync
from tophat.store.atomic import atomic_write_text
from tophat.store.firms import APEX, LUCID, TOPSTEP, TRADEIFY, get_firm
from tophat.store.mirrors import MirrorAccount, load_mirrors, merge_save_mirrors
from tophat.store.paths import COPIER_PLANS_DIR

# Apex fleet discipline mirrors the Topstep scheduler: stagger eval intake,
# one nuke slot per day (decorrelation), everyone else on the flip channel.
APEX_INTAKE_PER_DAY = 2
APEX_NUKE_SLOTS = 1


@dataclass
class Leader:
    """The solver's view of one Topstep account (built from snapshot + states)."""
    account_id: int
    name: str
    program: str            # "eval" | "funded"
    phase: str              # eval | funded | passed | blown | retired
    enabled: bool
    can_trade: bool
    signal_plan: str = ""   # non-empty = signal channel account
    days_traded: int = 0

    @property
    def live_eval(self) -> bool:
        return (self.program == "eval" and self.phase == "eval"
                and self.enabled and self.can_trade and not self.signal_plan)

    @property
    def live_funded(self) -> bool:
        return (self.phase == "funded" and self.enabled and self.can_trade
                and not self.signal_plan)

    @property
    def fresh_funded(self) -> bool:
        return self.live_funded and self.days_traded == 0


@dataclass
class PlanLine:
    action: str             # MAP | UNMAP | MOVE | SET_MULT | SET_CHANNEL | BUY | REQUEST_PAYOUT | ACTIVATE
    mirror_id: str
    detail: str
    reason: str


@dataclass
class Desired:
    leader_id: int | None
    channel: str
    multiplier: float
    nuke_today: bool = False    # holds today's apex nuke slot (stamps last_nuke_date)


@dataclass
class CopierPlan:
    date: str
    lines: list[PlanLine] = field(default_factory=list)
    desired: dict[str, Desired] = field(default_factory=dict)
    channels: list[dict] = field(default_factory=list)
    buys: list[dict] = field(default_factory=list)
    payout_queue: list[dict] = field(default_factory=list)
    hazards: list[dict] = field(default_factory=list)
    applied_at: str = ""
    # account_id -> display name, so the UI can label leaders by NAME everywhere
    # instead of a bare account number
    leader_names: dict[int, str] = field(default_factory=dict)


def _leader_name(leaders: dict[int, Leader], lid: int | None) -> str:
    if lid is None:
        return "-"
    l = leaders.get(lid)
    return l.name if l else f"#{lid}"


def build_plan(leaders: list[Leader], mirrors: dict[str, MirrorAccount],
               today: str) -> CopierPlan:
    plan = CopierPlan(date=today)
    lmap = {l.account_id: l for l in leaders}
    plan.leader_names = {l.account_id: l.name for l in leaders}
    mids = sorted(mirrors)                       # deterministic iteration everywhere

    # follower counts for balanced eval pack-following
    eval_leaders = sorted((l for l in leaders if l.live_eval),
                          key=lambda l: l.account_id)
    eval_load = {l.account_id: 0 for l in eval_leaders}
    for mid in mids:
        m = mirrors[mid]
        if m.leader_id in eval_load and m.phase == "eval":
            eval_load[m.leader_id] += 1

    # fresh funded leaders available for waiting mirrors (one twin per firm each)
    fresh = sorted((l for l in leaders if l.fresh_funded), key=lambda l: l.account_id)
    fresh_taken: dict[tuple[int, str], bool] = {}
    for mid in mids:
        m = mirrors[mid]
        if m.phase == "funded" and m.leader_id is not None:
            fresh_taken[(m.leader_id, m.firm)] = True

    signal_by_plan = {l.signal_plan: l for l in leaders
                      if l.signal_plan and l.enabled and l.can_trade}

    # ---- apex scheduling state (computed before per-mirror pairing) ----
    apex_funded = [mirrors[mid] for mid in mids
                   if mirrors[mid].firm == APEX.key and mirrors[mid].phase == "funded"
                   and mirrors[mid].enabled and not mirrors[mid].payout_ready]
    # a nuke-channel mirror whose last booked day was a win has landed its nuke
    # (mode transition emitted as SET_CHANNEL in _desire_apex)
    nuke_mode = [m for m in apex_funded
                 if m.channel != "flip" and not (m.channel == "nuke" and m.last_day_pnl > 0
                                                 and m.last_outcome_date)]
    # Stable within the day: a mirror already stamped with today's slot keeps it
    # (re-solving after apply must not reshuffle the rotation); otherwise longest-
    # since-nuke first, never-nuked ("" sorts first) ahead of everyone.
    nuke_mode.sort(key=lambda m: (0 if m.last_nuke_date == today else 1,
                                  m.last_nuke_date, m.mirror_id))
    nuke_today = {m.mirror_id for m in nuke_mode[:APEX_NUKE_SLOTS]}

    apex_evals = [mirrors[mid] for mid in mids
                  if mirrors[mid].firm == APEX.key and mirrors[mid].phase == "eval"
                  and mirrors[mid].enabled]
    # depth-first intake: most-advanced evals first (mirrors the Topstep pipeline)
    apex_evals.sort(key=lambda m: (-m.days_traded, m.mirror_id))
    intake_today = {m.mirror_id for m in apex_evals[:APEX_INTAKE_PER_DAY]}

    # ---- per-mirror desired mapping ----
    for mid in mids:
        m = mirrors[mid]
        if not m.enabled or m.terminal:
            continue
        firm = get_firm(m.firm)
        label = m.alias or mid

        if m.phase == "passed":
            plan.desired[mid] = Desired(None, m.channel, m.multiplier)
            if m.leader_id is not None:
                plan.lines.append(PlanLine("UNMAP", mid,
                                           f"{label}: remove from {_leader_name(lmap, m.leader_id)}",
                                           "eval passed - must stop copying"))
            plan.lines.append(PlanLine("ACTIVATE", mid,
                                       f"{label}: activate funded account at {firm.label}",
                                       "then TopHat: Accounts → Activate funded"))
            continue

        if m.payout_ready:
            plan.desired[mid] = Desired(None, m.channel, m.multiplier)
            if m.leader_id is not None:
                plan.lines.append(PlanLine("UNMAP", mid,
                                           f"{label}: remove from {_leader_name(lmap, m.leader_id)}",
                                           "payout eligible - park until paid"))
            plan.lines.append(PlanLine(
                "REQUEST_PAYOUT", mid,
                f"{label}: request ${mirror_sync.preview_payout(m):,.0f} at {firm.label}",
                "then TopHat: Mark Paid"))
            plan.payout_queue.append({
                "mirror_id": mid, "alias": label, "firm": firm.label,
                "amount": round(mirror_sync.preview_payout(m), 2)})
            continue

        if m.phase == "waiting":
            picked = None
            for l in fresh:
                if not fresh_taken.get((l.account_id, m.firm)):
                    picked = l
                    break
            if picked is not None:
                fresh_taken[(picked.account_id, m.firm)] = True
                plan.desired[mid] = Desired(picked.account_id, m.channel, 1.0)
                plan.lines.append(PlanLine(
                    "MAP", mid, f"{label}: follow {picked.name} @ 1.0x",
                    "fresh funded leader - paired for life"))
                plan.lines.append(PlanLine(
                    "ACTIVATE", mid, f"{label}: TopHat Accounts → Pair with {picked.name}",
                    "records the pairing so inference tracks it"))
            else:
                plan.desired[mid] = Desired(None, m.channel, m.multiplier)
            continue

        if m.firm == APEX.key:
            _desire_apex(plan, m, label, lmap, signal_by_plan, intake_today, nuke_today)
            continue

        # ---- clone firms (lucid / tradeify) ----
        if m.phase == "eval":
            want_mult = firm.copier_scale_eval
            lid = m.leader_id
            leader = lmap.get(lid) if lid is not None else None
            if leader is None or not leader.live_eval:
                # (re)map to the least-loaded live eval leader
                if eval_leaders:
                    pick = min(eval_leaders, key=lambda l: (eval_load[l.account_id],
                                                            l.account_id))
                    eval_load[pick.account_id] += 1
                    plan.desired[mid] = Desired(pick.account_id, "", want_mult)
                    verb = "MOVE" if lid is not None else "MAP"
                    why = ("its leader passed/stopped - ride the eval pack"
                           if lid is not None else "new eval - ride the eval pack")
                    plan.lines.append(PlanLine(
                        verb, mid,
                        f"{label}: follow {pick.name} @ {want_mult:g}x", why))
                else:
                    plan.desired[mid] = Desired(None, "", want_mult)
                    if lid is not None:
                        plan.lines.append(PlanLine(
                            "UNMAP", mid, f"{label}: remove mapping",
                            "no live eval leader available today"))
            else:
                plan.desired[mid] = Desired(lid, "", want_mult)
            if abs(m.multiplier - want_mult) > 1e-9:
                plan.lines.append(PlanLine(
                    "SET_MULT", mid, f"{label}: multiplier -> {want_mult:g}x",
                    f"{firm.label} eval copier scale"))
        else:  # funded clone — pairing is for life; only flag a dead leader
            leader = lmap.get(m.leader_id) if m.leader_id is not None else None
            if leader is not None and leader.live_funded:
                plan.desired[mid] = Desired(m.leader_id, "", 1.0)
            else:
                plan.desired[mid] = Desired(None, "", 1.0)
                if m.leader_id is not None:
                    plan.lines.append(PlanLine(
                        "UNMAP", mid,
                        f"{label}: remove from {_leader_name(lmap, m.leader_id)}",
                        "leader gone (blown/retired/disabled) - awaiting re-queue"))
            if abs(m.multiplier - 1.0) > 1e-9:
                plan.lines.append(PlanLine(
                    "SET_MULT", mid, f"{label}: multiplier -> 1x",
                    "funded accounts always copy at full size"))

    # ---- channels summary ----
    for key, l in sorted(signal_by_plan.items()):
        n = sum(1 for d in plan.desired.values() if d.leader_id == l.account_id)
        plan.channels.append({"channel": key, "leader": l.name,
                              "account_id": l.account_id, "followers": n})

    # ---- buy list ----
    plan.buys = _buy_list(leaders, mirrors)
    for b in plan.buys:
        plan.lines.append(PlanLine("BUY", "", f"{b['firm']}: buy {b['count']} eval(s)"
                                   f" (~${b['cost']:,.0f})", b["reason"]))

    return plan


def _desire_apex(plan: CopierPlan, m: MirrorAccount, label: str, lmap,
                 signal_by_plan: dict, intake_today: set, nuke_today: set) -> None:
    """Desired mapping for one Apex mirror: eval intake, nuke slot, or flip channel."""
    if m.phase == "eval":
        ch = signal_by_plan.get("apex-eval")
        if m.mirror_id in intake_today and ch is not None:
            plan.desired[m.mirror_id] = Desired(ch.account_id, "eval", m.multiplier)
            if m.leader_id != ch.account_id:
                plan.lines.append(PlanLine(
                    "MAP", m.mirror_id, f"{label}: follow {ch.name} (eval channel)",
                    f"intake slot ({APEX_INTAKE_PER_DAY}/day)"))
        else:
            plan.desired[m.mirror_id] = Desired(None, "eval", m.multiplier)
            if m.leader_id is not None:
                plan.lines.append(PlanLine(
                    "UNMAP", m.mirror_id, f"{label}: remove mapping",
                    "waiting for an intake slot" if ch is not None
                    else "no apex-eval signal channel configured"))
        return

    # funded PA: nuke-mode holders rotate through the single nuke slot
    landed = m.channel == "nuke" and m.last_day_pnl > 0 and bool(m.last_outcome_date)
    if landed and m.channel != "flip":
        plan.lines.append(PlanLine(
            "SET_CHANNEL", m.mirror_id, f"{label}: move to FLIP channel",
            "nuke landed - flips until payout"))
    if m.mirror_id in nuke_today:
        ch = signal_by_plan.get("apex-nuke")
        if ch is not None:
            plan.desired[m.mirror_id] = Desired(ch.account_id, "nuke", 1.0, nuke_today=True)
            if m.leader_id != ch.account_id:
                plan.lines.append(PlanLine(
                    "MAP", m.mirror_id, f"{label}: follow {ch.name} (nuke channel)",
                    "today's nuke slot (1/day rotation)"))
        else:
            plan.desired[m.mirror_id] = Desired(None, "nuke", 1.0)
    else:
        want_ch = "flip" if (landed or m.channel == "flip") else "nuke"
        if want_ch == "flip":
            ch = signal_by_plan.get("apex-flip")
            if ch is not None:
                plan.desired[m.mirror_id] = Desired(ch.account_id, "flip", 1.0)
                if m.leader_id != ch.account_id:
                    plan.lines.append(PlanLine(
                        "MAP", m.mirror_id, f"{label}: follow {ch.name} (flip channel)",
                        "flip mode - daily $325 flips"))
            else:
                plan.desired[m.mirror_id] = Desired(None, "flip", 1.0)
        else:
            # nuke-mode but not today's slot: idle, unmapped
            plan.desired[m.mirror_id] = Desired(None, "nuke", 1.0)
            if m.leader_id is not None:
                plan.lines.append(PlanLine(
                    "UNMAP", m.mirror_id, f"{label}: remove mapping",
                    "waiting for the nuke slot"))


def _buy_list(leaders: list[Leader], mirrors: dict[str, MirrorAccount]) -> list[dict]:
    out = []
    # Topstep: standing 10-eval pipeline
    ts_evals = sum(1 for l in leaders
                   if l.program == "eval" and l.phase == "eval" and l.enabled
                   and not l.signal_plan)
    need = TOPSTEP.eval_pipeline_target - ts_evals
    if need > 0:
        out.append({"firm": TOPSTEP.label, "count": need,
                    "cost": need * TOPSTEP.ticket_cost,
                    "reason": f"pipeline {ts_evals}/{TOPSTEP.eval_pipeline_target}"})
    # Lucid: min(7, 10 - funded) with total-cap awareness
    lu = [m for m in mirrors.values() if m.firm == LUCID.key and not m.terminal]
    lu_funded = sum(1 for m in lu if m.phase in ("funded", "waiting"))
    lu_evals = sum(1 for m in lu if m.phase in ("eval", "passed"))
    target = min(LUCID.eval_pipeline_target, (LUCID.max_total or 99) - lu_funded)
    need = min(target - lu_evals, (LUCID.max_total or 99) - lu_funded - lu_evals)
    if need > 0:
        out.append({"firm": LUCID.label, "count": need,
                    "cost": need * LUCID.ticket_cost,
                    "reason": f"evals {lu_evals}/{target} (cap 10 total, {lu_funded} funded)"})
    # Tradeify: standing 10
    td_evals = sum(1 for m in mirrors.values()
                   if m.firm == TRADEIFY.key and m.phase in ("eval", "passed"))
    need = TRADEIFY.eval_pipeline_target - td_evals
    if need > 0:
        out.append({"firm": TRADEIFY.label, "count": need,
                    "cost": need * TRADEIFY.ticket_cost,
                    "reason": f"pipeline {td_evals}/{TRADEIFY.eval_pipeline_target}"})
    # Apex: 10-eval cohort when PAs + 0.47*evals < 16 and none in flight
    ax = [m for m in mirrors.values() if m.firm == APEX.key and not m.terminal]
    pas = sum(1 for m in ax if m.phase in ("funded", "waiting"))
    evals = sum(1 for m in ax if m.phase in ("eval", "passed"))
    if evals == 0 and pas + 0.47 * evals < APEX.max_funded - 4:
        out.append({"firm": APEX.label, "count": 10,
                    "cost": 10 * APEX.ticket_cost,
                    "reason": f"new cohort ({pas} PAs, cap {APEX.max_funded}) - "
                              "payout-funded gate: buy only from banked payouts"})
    return out


# ------------------------------------------------------------ persistence
def _plan_path(date: str, base: Path | None = None) -> Path:
    d = base or COPIER_PLANS_DIR
    return d / f"{date}.json"


def plan_to_dict(plan: CopierPlan) -> dict:
    return {
        "date": plan.date,
        "lines": [asdict(x) for x in plan.lines],
        "desired": {k: asdict(v) for k, v in plan.desired.items()},
        "channels": plan.channels,
        "buys": plan.buys,
        "payout_queue": plan.payout_queue,
        "hazards": plan.hazards,
        "applied_at": plan.applied_at,
        "leader_names": {str(k): v for k, v in plan.leader_names.items()},
    }


def save_plan(plan: CopierPlan, base: Path | None = None) -> None:
    d = base or COPIER_PLANS_DIR
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_plan_path(plan.date, d), json.dumps(plan_to_dict(plan), indent=2))


def load_plan_dict(date: str, base: Path | None = None) -> dict | None:
    p = _plan_path(date, base)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def apply_plan(plan: CopierPlan, *, applied_at: str,
               base: Path | None = None) -> None:
    """Operator confirmed the Tradecopia edits: persist the desired mappings into
    the mirror store (inference now assumes them) and stamp the plan applied."""
    mirrors = load_mirrors()
    touched = []
    for mid, d in plan.desired.items():
        m = mirrors.get(mid)
        if m is None:
            continue
        m.leader_id = d.leader_id
        m.channel = d.channel
        m.multiplier = d.multiplier
        if d.nuke_today:
            m.last_nuke_date = plan.date
        touched.append(mid)
    if touched:
        merge_save_mirrors(mirrors, touched)
    plan.applied_at = applied_at
    save_plan(plan, base)


# ------------------------------------------------------------ live assembly
def leaders_from_pool(pool) -> list[Leader]:
    """Build the solver's leader view from the broker pool's cached snapshots."""
    from tophat.server import service
    from tophat.store.states import load_all
    states = load_all()
    out: list[Leader] = []
    for h in pool:
        if h.broker is None:
            continue
        try:
            snap = service.build_snapshot(h.broker, mode=h.mode, key=h.owner,
                                          owner=h.owner)
        except Exception:
            continue
        for r in snap["accounts"]:
            st = states.get(r["account_id"])
            out.append(Leader(
                account_id=r["account_id"], name=r["name"], program=r["program"],
                phase=r["phase"], enabled=r["enabled"],
                can_trade=bool(r["broker_can_trade"]) and not r["force_inactive"],
                signal_plan=r.get("signal_plan", ""),
                days_traded=st.days_traded if st else 0,
            ))
    return out


def build_today_plan(pool, today: str) -> CopierPlan:
    """Assemble inputs and solve. Merges applied_at if today's plan was applied."""
    leaders = leaders_from_pool(pool)
    mirrors = load_mirrors()
    plan = build_plan(leaders, mirrors, today)
    plan.hazards = mirror_sync.hazards(mirrors, today)
    # "no leader mapped" reads RECORDED state; when today's plan assigns one, the
    # mirror isn't stranded - the real action is applying the plan. Say that.
    for h in plan.hazards:
        d = plan.desired.get(h["mirror_id"])
        if d and d.leader_id is not None and "no leader mapped" in h["text"]:
            h["severity"] = "action"
            h["text"] = "leader assigned in today's plan - apply it in Tradecopia"
    rank = {"danger": 0, "action": 1, "warn": 2, "info": 3}
    plan.hazards.sort(key=lambda h: rank.get(h["severity"], 9))
    stored = load_plan_dict(today)
    if stored and stored.get("applied_at"):
        plan.applied_at = stored["applied_at"]
    return plan
