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


def _eval(**kw):
    d = dict(phase=Phase.EVAL, base_balance=50_000)
    d.update(kw)
    return AccountState(**d)


def test_evals_batched_two_per_day():
    accts = {i: _eval() for i in range(1, 4)}        # 3 evals enabled at once
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState())
    evals = [a for a in out.values() if a.action == "eval"]
    idle = [a for a in out.values() if a.action == "idle"]
    assert len(evals) == 2 and len(idle) == 1        # copy ≤2/day
    assert "waiting for an eval slot" in idle[0].note


def test_eval_slots_rotate_by_last_eval():
    accts = {1: _eval(), 2: _eval(), 3: _eval()}
    sched = ScheduleState(last_eval_date={1: "2026-06-21", 2: "2026-06-21"})  # 1,2 just ran
    out, _ = assign_day(accts, S, "2026-06-22", sched)
    assert out[3].action == "eval"                   # #3 waited longest -> gets a slot
    assert sched.last_eval_date[3] == "2026-06-22"   # rotation advances


def test_max_evals_per_day_float_is_coerced():
    s = TopHatSettings(); s.max_evals_per_day = 2.0  # UI sends floats
    out, _ = assign_day({1: _eval(), 2: _eval(), 3: _eval()}, s, "2026-06-22", ScheduleState())
    assert sum(a.action == "eval" for a in out.values()) == 2


def test_eval_pipeline_prioritizes_most_advanced():
    # Depth-first: an in-flight eval (more days traded) takes the slot over a fresh one,
    # so accounts are driven to completion instead of spread thin (docs/PROBABILITY.md §6).
    accts = {1: _eval(days_traded=0), 2: _eval(days_traded=2), 3: _eval(days_traded=1)}
    s = TopHatSettings(); s.max_evals_per_day = 1
    out, _ = assign_day(accts, s, "2026-06-22", ScheduleState())
    assert out[2].action == "eval"                  # furthest along wins the lone slot
    assert out[1].action == "idle" and out[3].action == "idle"
    assert "eval day 3" in out[2].note              # note reflects pipeline position


def test_eval_pipeline_two_slots_take_two_most_advanced():
    accts = {1: _eval(days_traded=0), 2: _eval(days_traded=3), 3: _eval(days_traded=2)}
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState())   # 2 slots
    assert out[2].action == "eval" and out[3].action == "eval"     # the two in-flight
    assert out[1].action == "idle"                                 # fresh one waits its turn


def test_eval_round_robin_when_pipeline_disabled():
    accts = {1: _eval(days_traded=0), 2: _eval(days_traded=2)}
    s = TopHatSettings(); s.max_evals_per_day = 1; s.eval_pipeline_depth_first = False
    sched = ScheduleState(last_eval_date={2: "2026-06-21"})  # #2 ran recently
    out, _ = assign_day(accts, s, "2026-06-22", sched)
    assert out[1].action == "eval"                  # round-robin: #1 (never ran) goes first
    assert out[2].action == "idle"                  # depth-first would have picked #2


def test_nuke_slot_tiebreak_prefers_earlier_enabled():
    accts = {1: funded(), 2: funded()}   # both fresh, both need their first nuke
    # #1 has the lower id but was enabled most recently -> #2 (already queued) keeps the slot.
    enabled_at = {1: 100.0, 2: 1.0}
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState(), enabled_at)
    assert out[2].action == "nuke"
    assert out[1].action == "idle"


def test_recovery_beats_a_newer_enable():
    # A mid-sequence recovery must keep priority even if it was enabled later.
    accts = {1: funded(), 2: funded(nuke_tries_this_cycle=1)}
    enabled_at = {1: 1.0, 2: 100.0}
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState(), enabled_at)
    assert out[2].action == "nuke" and "recovery" in out[2].note
    assert out[1].action == "idle"


def test_eval_slot_tiebreak_prefers_earlier_enabled():
    accts = {1: _eval(), 2: _eval(), 3: _eval()}
    # #1 has the lowest id but was enabled most recently -> it should be the one to wait,
    # not bump #2/#3 which were already active.
    enabled_at = {1: 100.0, 2: 1.0, 3: 1.0}
    out, _ = assign_day(accts, S, "2026-06-22", ScheduleState(), enabled_at)
    assert out[1].action == "idle"
    assert out[2].action == "eval" and out[3].action == "eval"


# --- mid-day slot consumption (2026-07-09 double-nuke incident) -----------------

def test_blown_nuke_slot_stays_consumed_for_the_day():
    # 09:45: #1 wins the day's only nuke slot, then gets liquidated and vanishes
    # from the fleet (canTrade=false). The freed slot must NOT rotate to #2 the
    # same day — one attempt per nuke slot per day. Tomorrow #2 gets it.
    accts = {1: funded(), 2: funded()}
    out, sched = assign_day(accts, S, "2026-07-09", ScheduleState())
    assert out[1].action == "nuke" and out[2].action == "idle"
    del accts[1]                                   # liquidated mid-morning
    out, sched = assign_day(accts, S, "2026-07-09", sched)
    assert out[2].action == "idle"
    out, _ = assign_day(accts, S, "2026-07-10", sched)
    assert out[2].action == "nuke"


def test_passed_eval_slot_stays_consumed_for_the_day():
    from tophat.store.config import TopHatSettings
    s = TopHatSettings(); s.max_evals_per_day = 1
    accts = {1: _eval(days_traded=1), 2: _eval()}
    out, sched = assign_day(accts, s, "2026-07-09", ScheduleState())
    assert out[1].action == "eval" and out[2].action == "idle"
    accts[1] = AccountState(phase=Phase.PASSED, base_balance=50_000)  # passed mid-morning
    out, sched = assign_day(accts, s, "2026-07-09", sched)
    assert out[2].action == "idle"                 # slot consumed by the pass
    out, _ = assign_day(accts, s, "2026-07-10", sched)
    assert out[2].action == "eval"


def test_slot_holder_keeps_slot_across_ticks():
    # assign_day runs every ~30s tick; the stamp must not change the sort so the
    # un-fired slot churns to a different account each tick.
    accts = {1: _eval(), 2: _eval(), 3: _eval()}
    out1, sched = assign_day(accts, S, "2026-07-09", ScheduleState())
    winners1 = {a for a, o in out1.items() if o.action == "eval"}
    out2, sched = assign_day(accts, S, "2026-07-09", sched)
    winners2 = {a for a, o in out2.items() if o.action == "eval"}
    assert winners1 == winners2


def test_holder_keeps_slot_over_late_enabled_advanced_eval():
    # A more-advanced eval enabled mid-morning must not bump the account already
    # holding today's stamp (stability beats depth-first within the day).
    from tophat.store.config import TopHatSettings
    s = TopHatSettings(); s.max_evals_per_day = 1
    accts = {1: _eval()}
    out, sched = assign_day(accts, s, "2026-07-09", ScheduleState())
    assert out[1].action == "eval"
    accts[2] = _eval(days_traded=3)               # enabled later, further along
    out, sched = assign_day(accts, s, "2026-07-09", sched,
                            enabled_at={1: 1.0, 2: 100.0})
    assert out[1].action == "eval" and out[2].action == "idle"
