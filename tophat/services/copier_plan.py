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

# Non-lockstep clone evals (Tradeify 0.8x) follow the fleet's eval discipline
# too: at most this many riding the pack on any day (operator decision
# 2026-07-09 — same 2-evals/day rule as the Topstep scheduler and the Apex
# intake). Lockstep 1.0x twins (Lucid) need no gate: a twin only trades when
# its own leader holds a Topstep eval slot, so it inherits that pacing — which
# is exactly why pairing targets the slot HOLDERS (the depth-first drivers),
# never an alive-but-idle leader (operator report 2026-07-13: twins spread
# least-loaded across the pack sat on idle evals and traded nothing).
CLONE_INTAKE_PER_DAY = 2

# Lucid twin throttle: stop buying twins while this many passed twins already
# wait for a fleet-wide funded slot (5). 2026-07-11 sweep: the original
# stop-at-1 was too tight — 3 is +$8k median/yr at two logins (funded-twin
# occupancy 53%→58%) and statistically tied with unthrottled at one login,
# while unthrottled at two logins wastes ~$12k/yr in twins that rot in the
# queue (research/forecast_replenishment_sweep.py).
LUCID_MAX_WAITING = 3

# Eval-finisher sizing (operator-approved 2026-07-06; STATS_AUDIT addendum).
# The leader's eval win day is 15.5pt x 5 minis = $1,550 gross; one follower
# mini nets ~$303 after ~$7 RT commission. When a scaled clone eval (Tradeify
# 0.8x) can FINISH with fewer minis, cut the multiplier to just cover
# max(target, best_day/consistency) + margin: the win probability is the
# leader's bracket either way, but a red finish day loses less, leaving more
# retries above the trailing floor (+3.4pp pass, +$62/ticket measured).
# Mini granularity only — an MNQ cross-copy finisher loses its edge to micro
# commissions (measured: below even the no-finisher baseline).
LEADER_EVAL_DAY_GROSS = 1_550.0
LEADER_EVAL_CONTRACTS = 5
FINISHER_MINI_NET = LEADER_EVAL_DAY_GROSS / LEADER_EVAL_CONTRACTS - 7.0
FINISHER_MARGIN = 60.0


def _eval_scale(firm, m: MirrorAccount) -> float:
    """Copier multiplier for a clone-firm eval mirror: the firm's standard
    scale until the account is close enough to finish smaller."""
    base = firm.copier_scale_eval
    if base >= 1.0 or m.days_traded == 0 or m.equity <= 0:
        return base            # pure clones (Lucid) and fresh evals: as-is
    bar = firm.eval_target
    if firm.eval_consistency:
        bar = max(bar, m.eval_best_day / firm.eval_consistency)
    remaining = bar + FINISHER_MARGIN - m.equity
    if remaining >= base * LEADER_EVAL_DAY_GROSS:
        return base            # still needs a full day - no finisher yet
    minis = max(1, -(-int(remaining) // int(FINISHER_MINI_NET)))  # ceil
    return min(base, minis / LEADER_EVAL_CONTRACTS)


@dataclass
class Leader:
    """The solver's view of one Topstep account (built from snapshot + states)."""
    account_id: int
    name: str
    program: str            # "eval" | "funded"
    phase: str              # eval | funded | passed | blown | retired
    enabled: bool
    can_trade: bool         # OPERATIONAL tradability (snapshot's can_trade): folds in
                            # the below-MLL check + force_inactive, NOT the raw broker
                            # canTrade — Topstep keeps canTrade=true on some dead
                            # below-floor accounts and those must never look live here
    signal_plan: str = ""   # non-empty = signal channel account
    practice: bool = False  # PRAC-* ticket: may carry a signal channel but must never
                            # be an eval/funded leader or count toward the pipeline
    days_traded: int = 0
    near_floor: bool = False  # within one eval-stop of the trailing floor -> its next
                              # entry fires WITHOUT a protective stop (auto-liquidation)
    owner: str = ""         # API-key login the account lives under; the eval
                            # pipeline target is maintained PER login (each login
                            # runs its own 2 eval slots/day)

    @property
    def live_eval(self) -> bool:
        return (self.program == "eval" and self.phase == "eval"
                and self.enabled and self.can_trade
                and not self.signal_plan and not self.practice)

    @property
    def pipeline_eval(self) -> bool:
        """Occupies a Topstep eval pipeline slot. PHASE is the source of truth
        here, not can_trade: below-MLL accounts are auto-marked blown by the
        snapshot (so they drop out via phase), while a DLL-locked account —
        red day, still above the trailing floor — may report canTrade=false
        for the rest of the day yet is alive and keeps its slot. Gating this
        on can_trade would recommend replacement buys on every red day."""
        return (self.program == "eval" and self.phase == "eval"
                and self.enabled and not self.signal_plan and not self.practice)

    @property
    def live_funded(self) -> bool:
        return (self.phase == "funded" and self.enabled and self.can_trade
                and not self.signal_plan and not self.practice)

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
               today: str, *, eval_slots: int = 2) -> CopierPlan:
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

    # Tomorrow's DRIVERS: per login, the evals the depth-first scheduler will
    # actually hand the day's `eval_slots` to (most advanced first, the
    # scheduler's own ordering). A twin parked on any other live leader sits
    # through a day of nothing — pairing must target these.
    by_owner: dict[str, list[Leader]] = {}
    for l in eval_leaders:
        by_owner.setdefault(l.owner, []).append(l)
    drivers: set[int] = set()
    for lst in by_owner.values():
        lst.sort(key=lambda l: (-l.days_traded, l.account_id))
        drivers.update(l.account_id for l in lst[:max(0, int(eval_slots))])

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

    # ---- scaled-clone (non-lockstep) eval intake, per firm ----
    scaled_evals: dict[str, list[MirrorAccount]] = {}
    for mid in mids:
        m = mirrors[mid]
        if (m.phase == "eval" and m.enabled and not m.terminal
                and get_firm(m.firm).copier_scale_eval < 1.0 - 1e-9):
            scaled_evals.setdefault(m.firm, []).append(m)
    clone_intake_today: set[str] = set()
    for lst in scaled_evals.values():
        lst.sort(key=lambda m: (-m.days_traded, m.mirror_id))
        clone_intake_today.update(m.mirror_id for m in lst[:CLONE_INTAKE_PER_DAY])

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
                                       "then TopHat: Accounts → Activate funded"
                                       " (Topstep accounts: enable Auto OCO Brackets"
                                       " in the new account's platform settings)"))
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
            want_mult = _eval_scale(firm, m)
            # A leader within one stop of its floor fires its next entry stopless
            # (should_omit_stop -> broker auto-liquidation). Lucid copies losses 1:1
            # and dies in lockstep, so that's safe; a non-lockstep follower (Tradeify
            # 0.8x) neither dies in lockstep nor has a DLL backstop, so a copied
            # stopless entry can run unbounded. Never pair it to a near-floor leader.
            guard = firm.copier_scale_eval < 1.0 - 1e-9
            if guard and mid not in clone_intake_today:
                plan.desired[mid] = Desired(None, "", want_mult)
                if m.leader_id is not None:
                    plan.lines.append(PlanLine(
                        "UNMAP", mid, f"{label}: remove mapping",
                        f"waiting for an intake slot ({CLONE_INTAKE_PER_DAY}/day)"))
                continue
            lid = m.leader_id
            leader = lmap.get(lid) if lid is not None else None
            unsafe_current = leader is not None and guard and leader.near_floor
            # A lockstep twin on an alive-but-idle leader (no eval slot under
            # the depth-first scheduler) trades NOTHING all day — sticky
            # pairing must not park it there while a driver is available.
            idle_current = bool(leader is not None and leader.live_eval
                                and not guard and drivers
                                and leader.account_id not in drivers)
            if leader is None or not leader.live_eval or unsafe_current or idle_current:
                # (re)map to a live eval leader (safe leaders only)
                avail = [l for l in eval_leaders if not (guard and l.near_floor)]
                if avail:
                    if guard:
                        # A non-lockstep rider must sit on a leader the depth-
                        # first scheduler is actually DRIVING (most advanced =
                        # today's slot holders) — parked on an idle fresh
                        # leader, its intake slot would trade nothing.
                        pick = min(avail, key=lambda l: (-l.days_traded,
                                                         eval_load[l.account_id],
                                                         l.account_id))
                    else:
                        # lockstep twins ride the drivers (tomorrow's slot
                        # holders), least loaded first so they spread across
                        # all of them before doubling up
                        pick = min(avail, key=lambda l: (l.account_id not in drivers,
                                                         eval_load[l.account_id],
                                                         l.account_id))
                    eval_load[pick.account_id] += 1
                    plan.desired[mid] = Desired(pick.account_id, "", want_mult)
                    verb = "MOVE" if lid is not None else "MAP"
                    if unsafe_current:
                        why = ("leader near its floor (would fire stopless) - "
                               "remap to a safe eval leader")
                    elif idle_current:
                        why = ("leader holds no eval slot (depth-first pipeline) "
                               "- ride a scheduled leader")
                    elif lid is not None:
                        why = "its leader passed/stopped - ride the eval pack"
                    else:
                        why = "new eval - ride the eval pack"
                    plan.lines.append(PlanLine(
                        verb, mid,
                        f"{label}: follow {pick.name} @ {want_mult:g}x", why))
                else:
                    plan.desired[mid] = Desired(None, "", want_mult)
                    if lid is not None:
                        reason = ("all live eval leaders near their floor (would fire "
                                  "stopless) - hold flat" if guard and eval_leaders
                                  else "no live eval leader available today")
                        plan.lines.append(PlanLine(
                            "UNMAP", mid, f"{label}: remove mapping", reason))
            else:
                plan.desired[mid] = Desired(lid, "", want_mult)
            if abs(m.multiplier - want_mult) > 1e-9:
                why = (f"{firm.label} eval finisher - a smaller last day still "
                       "passes and cuts the loss if it goes red"
                       if want_mult < firm.copier_scale_eval - 1e-9
                       else f"{firm.label} eval copier scale")
                plan.lines.append(PlanLine(
                    "SET_MULT", mid, f"{label}: multiplier -> {want_mult:g}x", why))
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
        why = b["reason"]
        if b["firm"] == TOPSTEP.label:
            # New Topstep accounts ship with "Position Brackets" — the API
            # bracket rejection that cost three evals their day on 2026-07-09.
            why += " — enable Auto OCO Brackets on each new account"
        plan.lines.append(PlanLine("BUY", "", f"{b['firm']}: buy {b['count']} eval(s)"
                                   f" (~${b['cost']:,.0f})", why))

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
                        "flip mode - daily $285 flips"))
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
    # Topstep: standing eval pipeline PER LOGIN (2026-07-11 sweep: each login
    # runs its own 2 eval slots/day, so each needs its own 6-standing float —
    # a fleet-wide 10 split across two logins is ~5/login, which starves the
    # slots mid-week, −$13-15k median/yr). A brand-new login shows up here
    # only once its first account exists in the pool snapshot.
    owners = sorted({l.owner for l in leaders if not l.signal_plan}) or [""]
    for owner in owners:
        # pipeline_eval: dead accounts drop out via the auto-blown phase flip
        # (MLL is the source of truth), practice/signal tickets never counted,
        # and DLL-locked-but-alive accounts still hold their slot.
        ts_evals = sum(1 for l in leaders if l.owner == owner and l.pipeline_eval)
        need = TOPSTEP.eval_pipeline_target - ts_evals
        if need > 0:
            login = f"login {owner}: " if owner and len(owners) > 1 else ""
            out.append({"firm": TOPSTEP.label, "count": need,
                        "cost": need * TOPSTEP.ticket_cost, "owner": owner,
                        "reason": f"{login}pipeline {ts_evals}/"
                                  f"{TOPSTEP.eval_pipeline_target}"})
    # Lucid: standing 10. Funded accounts do NOT consume slots - the firm
    # deletes the eval account at funded activation (operator 2026-07-08),
    # so the 10-account cap only ever holds evals. Twin throttle: stop
    # stacking twins while LUCID_MAX_WAITING passes already queue for the
    # fleet-wide funded cap (loosened 1 -> 3, 2026-07-11 sweep).
    lu = [m for m in mirrors.values() if m.firm == LUCID.key and not m.terminal]
    lu_evals = sum(1 for m in lu if m.phase in ("eval", "passed"))
    lu_waiting = sum(1 for m in lu if m.phase in ("passed", "waiting"))
    target = min(LUCID.eval_pipeline_target, LUCID.max_total or 99)
    need = target - lu_evals if lu_waiting < LUCID_MAX_WAITING else 0
    if need > 0:
        out.append({"firm": LUCID.label, "count": need,
                    "cost": need * LUCID.ticket_cost,
                    "reason": f"pipeline {lu_evals}/{target}"})
    # Tradeify: standing 6 (2026-07-11 sweep; intake stays 2/day)
    td_evals = sum(1 for m in mirrors.values()
                   if m.firm == TRADEIFY.key and m.phase in ("eval", "passed"))
    need = TRADEIFY.eval_pipeline_target - td_evals
    if need > 0:
        out.append({"firm": TRADEIFY.label, "count": need,
                    "cost": need * TRADEIFY.ticket_cost,
                    "reason": f"pipeline {td_evals}/{TRADEIFY.eval_pipeline_target}"})
    # Apex: weekly top-up to 8 standing evals while PA headroom remains.
    # Replaced the buy-10-when-all-resolved cohort rule (2026-07-11 sweep:
    # waiting for the whole cohort to resolve starved the 2/day intake between
    # cohorts — top-up lifts PA occupancy 35%→40% and payouts 97→111/yr,
    # +$20k median for +$2.5k/yr in tickets). Still no payout-funded cash
    # gate (operator decision 2026-07-09: the gate starved recovery in bad
    # runs, P10 −$1K gated vs +$43K ungated).
    ax = [m for m in mirrors.values() if m.firm == APEX.key and not m.terminal]
    pas = sum(1 for m in ax if m.phase in ("funded", "waiting"))
    evals = sum(1 for m in ax if m.phase in ("eval", "passed"))
    need = APEX.eval_pipeline_target - evals
    if need > 0 and pas < APEX.max_funded - 4:
        out.append({"firm": APEX.label, "count": need,
                    "cost": need * APEX.ticket_cost,
                    "reason": f"pipeline {evals}/{APEX.eval_pipeline_target} "
                              f"({pas} PAs, cap {APEX.max_funded})"})
    return out


# ------------------------------------------------------------ persistence
def _plan_path(date: str, base: Path | None = None) -> Path:
    from tophat.store import tenant
    d = base or tenant.resolve(COPIER_PLANS_DIR)
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
    from tophat.store import tenant
    d = base or tenant.resolve(COPIER_PLANS_DIR)
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
    from tophat.engine import room_to_floor
    from tophat.server import service
    from tophat.store.config import load_settings
    from tophat.store.states import load_all
    states = load_all()
    cfg = load_settings().to_account_config()
    eval_stop = cfg.eval_stop_pts * cfg.eval_contracts * cfg.point_value
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
            # "near floor" == within one eval-stop of the trailing floor, so the
            # leader's next entry omits its protective stop. Uses the same broker
            # balance the live should_omit_stop path uses.
            near_floor = bool(st) and room_to_floor(
                cfg, st, r.get("balance")) <= eval_stop + 1e-9
            out.append(Leader(
                account_id=r["account_id"], name=r["name"], program=r["program"],
                phase=r["phase"], enabled=r["enabled"],
                # The snapshot's operational verdict, not raw broker canTrade: it
                # already folds in force_inactive AND the below-MLL check, so a
                # dead-but-canTrade Topstep account can't pose as a live leader.
                can_trade=bool(r["can_trade"]),
                signal_plan=r.get("signal_plan", ""),
                practice=bool(r.get("practice")),
                days_traded=st.days_traded if st else 0,
                near_floor=near_floor,
                owner=str(h.owner or ""),
            ))
    return out


def build_today_plan(pool, today: str) -> CopierPlan:
    """Assemble inputs and solve. Merges applied_at if today's plan was applied."""
    from tophat.store.config import load_settings
    leaders = leaders_from_pool(pool)
    mirrors = load_mirrors()
    plan = build_plan(leaders, mirrors, today,
                      eval_slots=int(load_settings().max_evals_per_day))
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
