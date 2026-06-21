"""Daily decorrelation scheduler: who nukes today, and flip stagger slots.

Rules (see docs/STRATEGY.md / docs/BUILD_PLAN.md):
  - At most `max_nukes_per_day` accounts nuke on any calendar day (decorrelation).
  - A pending day-2 recovery (mid-sequence) takes priority for a nuke slot.
  - Otherwise nuke slots go round-robin to the funded accounts that have waited
    longest since their last nuke.
  - Funded accounts not nuking and past their nuke (or in a flip cycle) flip, each
    assigned a staggered entry time so the fleet's flips don't all fire together.
  - Nuke-cycle accounts that don't win a nuke slot idle (they can't flip yet).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from tophat.engine import TERMINAL, AccountState, Phase, is_nuke_cycle
from tophat.store.config import TopHatSettings
from tophat.store.paths import SCHEDULE_FILE


@dataclass
class ScheduleState:
    last_nuke_date: dict[int, str] = field(default_factory=dict)  # account_id -> YYYY-MM-DD
    last_run_date: str = ""


def load_schedule(path: Path = SCHEDULE_FILE) -> ScheduleState:
    if not path.exists():
        return ScheduleState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ScheduleState(
        last_nuke_date={int(k): v for k, v in raw.get("last_nuke_date", {}).items()},
        last_run_date=raw.get("last_run_date", ""),
    )


def save_schedule(s: ScheduleState, path: Path = SCHEDULE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_nuke_date": {str(k): v for k, v in s.last_nuke_date.items()},
        "last_run_date": s.last_run_date,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def assign_day(
    accounts: dict[int, AccountState],
    settings: TopHatSettings,
    today: str,
    sched: ScheduleState | None = None,
) -> tuple[dict[int, Assignment], ScheduleState]:
    """Return per-account assignments for `today` and the updated schedule state.

    `accounts` should already be filtered to enabled + tradeable accounts.
    """
    sched = sched or load_schedule()
    out: dict[int, Assignment] = {}

    nuke_candidates: list[int] = []
    flip_accounts: list[int] = []
    for aid, st in accounts.items():
        if st.phase in TERMINAL:
            out[aid] = Assignment(aid, st.phase.value, "", st.phase.value)
        elif st.phase == Phase.EVAL:
            out[aid] = Assignment(aid, "eval", settings.nuke_entry_time, "eval day")
        elif _needs_nuke(st):
            nuke_candidates.append(aid)
        else:
            flip_accounts.append(aid)

    # Order nuke candidates: pending recoveries first, then longest-since-last-nuke.
    def sort_key(aid: int):
        st = accounts[aid]
        recovery = st.nuke_tries_this_cycle >= 1   # mid-sequence, must continue
        last = sched.last_nuke_date.get(aid, "")    # "" sorts first => never nuked
        return (0 if recovery else 1, last, aid)

    nuke_candidates.sort(key=sort_key)
    slots = max(0, settings.max_nukes_per_day)
    nuking = set(nuke_candidates[:slots])

    for aid in nuke_candidates:
        st = accounts[aid]
        kind = "renuke" if st.payouts_taken >= 2 else "nuke"
        if aid in nuking:
            recovery = st.nuke_tries_this_cycle >= 1
            note = "day-2 recovery" if recovery else "nuke slot (one/day)"
            out[aid] = Assignment(aid, kind, settings.nuke_entry_time, note)
            sched.last_nuke_date[aid] = today
        else:
            out[aid] = Assignment(aid, "idle", "", "waiting for a nuke slot")

    # Stagger flips across the configured entry times.
    times = settings.flip_stagger_times or [settings.nuke_entry_time]
    for i, aid in enumerate(sorted(flip_accounts)):
        out[aid] = Assignment(aid, "flip", times[i % len(times)],
                              f"flip slot {i % len(times) + 1}")

    sched.last_run_date = today
    return out, sched
