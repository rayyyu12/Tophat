"""Unit tests for the decorrelation scheduler."""

from tophat.engine import AccountState, Phase
from tophat.services.scheduler import ScheduleState, assign_day
from tophat.store.config import TopHatSettings

S = TopHatSettings()  # defaults: 1 nuke/day, 5 stagger slots


def funded(**kw):
    d = dict(phase=Phase.FUNDED, equity=0.0, peak_equity_eod=0.0, base_balance=0.0)
    d.update(kw)
    return AccountState(**d)


def test_one_nuke_per_day_rest_idle():
    accts = {i: funded() for i in range(1, 5)}     # all need their first nuke
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState())
    nukes = [a for a in out.values() if a.action == "nuke"]
    idle = [a for a in out.values() if a.action == "idle"]
    assert len(nukes) == 1 and len(idle) == 3


def test_recovery_has_nuke_priority():
    accts = {
        1: funded(),                                # fresh
        2: funded(nuke_tries_this_cycle=1),         # mid-sequence recovery
    }
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState())
    assert out[2].action == "nuke" and "recovery" in out[2].note
    assert out[1].action == "idle"


def test_round_robin_by_last_nuke():
    accts = {1: funded(), 2: funded()}
    sched = ScheduleState(last_nuke_date={1: "2026-06-21"})  # #1 nuked recently
    out, _ = assign_day(accts, S, "2026-06-22", sched)
    assert out[2].action == "nuke"                 # #2 waited longer
    assert out[1].action == "idle"


def test_flips_get_staggered_times():
    accts = {i: funded(nuke_hit_this_cycle=True) for i in range(1, 4)}  # past nuke -> flip
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState())
    times = sorted(a.entry_time for a in out.values())
    assert times == S.flip_stagger_times[:3]       # distinct slots
    assert all(a.action == "flip" for a in out.values())


def test_eval_and_terminal_assignments():
    accts = {
        1: AccountState(phase=Phase.EVAL, base_balance=50_000),
        2: funded(phase=Phase.BLOWN),
        3: funded(phase=Phase.PASSED),
    }
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState())
    assert out[1].action == "eval"
    assert out[2].action == "blown" and out[3].action == "passed"


def test_max_nukes_per_day_zero():
    s = TopHatSettings(); s.max_nukes_per_day = 0
    out, _ = assign_day({1: funded(), 2: funded()}, s, "2026-06-22", ScheduleState())
    assert all(a.action == "idle" for a in out.values())
