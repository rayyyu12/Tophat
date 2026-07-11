"""Daily decorrelation scheduler: who nukes today, and flip stagger slots.

Rules (see docs/STRATEGY.md / docs/BUILD_PLAN.md):
  - At most `max_nukes_per_day` accounts nuke on any calendar day (decorrelation).
  - A pending day-2 recovery (mid-sequence) takes priority for a nuke slot.
  - Otherwise nuke slots go round-robin to the funded accounts that have waited
    longest since their last nuke.
  - At most `max_evals_per_day` evals trade on any day. By default the eval slots
    run a DEPTH-FIRST PIPELINE: the most-advanced evals (highest `days_traded`) are
    driven to pass/blow before fresh ones start, which front-loads funded accounts
    at no cost to per-account pass probability (docs/PROBABILITY.md §6). Set
    `eval_pipeline_depth_first=False` to fall back to round-robin (spread evenly).
  - Funded accounts not nuking and past their nuke (or in a flip cycle) flip, each
    assigned a staggered entry time so the fleet's flips don't all fire together.
  - Nuke-cycle accounts that don't win a nuke slot idle (they can't flip yet).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from tophat.engine import TERMINAL, AccountState, Phase, is_nuke_cycle
from tophat.store import tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.config import TopHatSettings
from tophat.store.paths import SCHEDULE_FILE


@dataclass
class ScheduleState:
    last_nuke_date: dict[int, str] = field(default_factory=dict)  # account_id -> YYYY-MM-DD
    last_eval_date: dict[int, str] = field(default_factory=dict)  # account_id -> YYYY-MM-DD
    # account_id -> credential owner that stamped it. The schedule file is shared
    # across a tenant's API keys while the slot caps are PER KEY, so consumed-slot
    # accounting (ghosts) must know whose slot a stamp was. Missing entries are
    # attributed to the asking owner (pre-upgrade stamps, single-key tenants).
    slot_owner: dict[int, str] = field(default_factory=dict)
    last_run_date: str = ""


def load_schedule(path: Path | None = None) -> ScheduleState:
    path = tenant.resolve(SCHEDULE_FILE) if path is None else path
    if not path.exists():
        return ScheduleState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ScheduleState(
        last_nuke_date={int(k): v for k, v in raw.get("last_nuke_date", {}).items()},
        last_eval_date={int(k): v for k, v in raw.get("last_eval_date", {}).items()},
        slot_owner={int(k): v for k, v in raw.get("slot_owner", {}).items()},
        last_run_date=raw.get("last_run_date", ""),
    )


def save_schedule(s: ScheduleState, path: Path | None = None) -> None:
    path = tenant.resolve(SCHEDULE_FILE) if path is None else path
    payload = {
        "last_nuke_date": {str(k): v for k, v in s.last_nuke_date.items()},
        "last_eval_date": {str(k): v for k, v in s.last_eval_date.items()},
        "slot_owner": {str(k): v for k, v in s.slot_owner.items()},
        "last_run_date": s.last_run_date,
    }
    atomic_write_text(path, json.dumps(payload, indent=2))


@dataclass
class Assignment:
    account_id: int
    action: str           # "nuke" | "renuke" | "flip" | "eval" | "idle" | "retire"
    entry_time: str        # ET "HH:MM"
    note: str = ""


def _needs_nuke(st: AccountState) -> bool:
    return (st.phase == Phase.FUNDED
            and is_nuke_cycle(st.payouts_taken)
            and not st.nuke_hit_this_cycle)


def _ghost_slots(stamps: dict[int, str], today: str, candidates: set[int],
                 slot_owner: dict[int, str], owner: str) -> int:
    """Slots already consumed today by THIS owner's no-longer-candidate accounts.

    An account stamped with today's slot that has since blown, passed, vanished
    from the broker (Topstep flips canTrade=false on liquidation, delists a few
    minutes later) or been disabled keeps its slot CONSUMED. Re-awarding the
    freed slot would fire a second correlated account inside the entry grace
    window — the 2026-07-09 incident where the next funded account nuked at
    09:49 after the slot holder was liquidated at 09:48. Stamps made by a
    DIFFERENT credential never count: each API key runs its own caps."""
    return sum(1 for aid, d in stamps.items()
               if d == today and aid not in candidates
               and slot_owner.get(aid, owner) == owner)


def _holders_first(ordered: list[int], stamps: dict[int, str], today: str) -> list[int]:
    """Stable re-order: accounts already stamped with today's slot keep it.

    assign_day runs on every ~30s automation tick; without this, the stamp
    itself changes the sort key, so a not-yet-due slot churns to a different
    account each tick (each new holder gets stamped, pushing it behind the
    still-unstamped rest). The holder keeps the slot until its entry time; the
    fire itself is one attempt per day (a failure sets last_fire_date)."""
    held = [a for a in ordered if stamps.get(a) == today]
    return held + [a for a in ordered if stamps.get(a) != today]


def assign_day(
    accounts: dict[int, AccountState],
    settings: TopHatSettings,
    today: str,
    sched: ScheduleState | None = None,
    enabled_at: dict[int, float] | None = None,
    owner: str = "",
) -> tuple[dict[int, Assignment], ScheduleState]:
    """Return per-account assignments for `today` and the updated schedule state.

    `accounts` should already be filtered to enabled + tradeable accounts.
    `enabled_at` (account_id -> epoch enabled) breaks eval-slot ties so a freshly
    enabled account waits behind ones already holding a slot, rather than bumping them.
    `owner` is the credential these accounts belong to — slot-consumption
    accounting is per API key even though the schedule file is shared.
    """
    sched = sched or load_schedule()
    enabled_at = enabled_at or {}
    out: dict[int, Assignment] = {}

    eval_candidates: list[int] = []
    nuke_candidates: list[int] = []
    flip_accounts: list[int] = []
    for aid, st in accounts.items():
        if st.phase in TERMINAL:
            out[aid] = Assignment(aid, st.phase.value, "", st.phase.value)
        elif st.phase == Phase.EVAL:
            eval_candidates.append(aid)
        elif _needs_nuke(st):
            nuke_candidates.append(aid)
        else:
            flip_accounts.append(aid)

    # Eval batching: copy at most `max_evals_per_day` evals on any day (STRATEGY §1 —
    # limits correlated eval exposure). Depth-first pipeline (default): slots go to the
    # MOST-ADVANCED evals first (highest days_traded) so in-flight accounts are driven to
    # pass/blow before fresh ones start — front-loads funded accounts at no probability
    # cost (docs/PROBABILITY.md §6). Round-robin fallback spreads slots by longest wait.
    # Ties either way: longest since last eval, earliest enabled (a freshly enabled
    # account waits rather than bumping an active one), then id.
    if settings.eval_pipeline_depth_first:
        eval_candidates.sort(key=lambda a: (
            -accounts[a].days_traded, sched.last_eval_date.get(a, ""), enabled_at.get(a, 0.0), a))
    else:
        eval_candidates.sort(key=lambda a: (sched.last_eval_date.get(a, ""), enabled_at.get(a, 0.0), a))
    eval_slots = max(0, int(settings.max_evals_per_day)
                     - _ghost_slots(sched.last_eval_date, today, set(eval_candidates),
                                    sched.slot_owner, owner))
    eval_candidates = _holders_first(eval_candidates, sched.last_eval_date, today)
    evaling = set(eval_candidates[:eval_slots])
    for aid in eval_candidates:
        if aid in evaling:
            out[aid] = Assignment(aid, "eval", settings.nuke_entry_time,
                                  f"eval day {accounts[aid].days_traded + 1} ({eval_slots}/day)")
            sched.last_eval_date[aid] = today
            sched.slot_owner[aid] = owner
        else:
            out[aid] = Assignment(aid, "idle", "", "waiting for an eval slot")

    # Order nuke candidates: pending recoveries first, then longest-since-last-nuke,
    # then earliest enabled (a freshly enabled account waits behind an already-queued
    # one rather than bumping it), then id.
    def sort_key(aid: int):
        st = accounts[aid]
        recovery = st.nuke_tries_this_cycle >= 1   # mid-sequence, must continue
        last = sched.last_nuke_date.get(aid, "")    # "" sorts first => never nuked
        return (0 if recovery else 1, last, enabled_at.get(aid, 0.0), aid)

    nuke_candidates.sort(key=sort_key)
    slots = max(0, int(settings.max_nukes_per_day)
                - _ghost_slots(sched.last_nuke_date, today, set(nuke_candidates),
                               sched.slot_owner, owner))
    nuke_candidates = _holders_first(nuke_candidates, sched.last_nuke_date, today)
    nuking = set(nuke_candidates[:slots])

    for aid in nuke_candidates:
        st = accounts[aid]
        kind = "renuke" if st.payouts_taken >= 2 else "nuke"
        if aid in nuking:
            recovery = st.nuke_tries_this_cycle >= 1
            note = "day-2 recovery" if recovery else "nuke slot (one/day)"
            out[aid] = Assignment(aid, kind, settings.nuke_entry_time, note)
            sched.last_nuke_date[aid] = today
            sched.slot_owner[aid] = owner
        else:
            out[aid] = Assignment(aid, "idle", "", "waiting for a nuke slot")

    # Stagger flips across the configured entry times.
    times = settings.flip_stagger_times or [settings.nuke_entry_time]
    for i, aid in enumerate(sorted(flip_accounts)):
        out[aid] = Assignment(aid, "flip", times[i % len(times)],
                              f"flip slot {i % len(times) + 1}")

    sched.last_run_date = today
    return out, sched
