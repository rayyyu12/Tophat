"""Copier plan solver: pairing, apex scheduling, buy list, diff semantics."""

from tophat.services import copier_plan as cp
from tophat.services import mirror_sync
from tophat.store import mirrors as MS

TODAY = "2026-07-06"


def L(aid, program="eval", phase="eval", enabled=True, can_trade=True,
      signal_plan="", days=0, name=None, near_floor=False, owner=""):
    return cp.Leader(account_id=aid, name=name or f"TS-{aid}", program=program,
                     phase=phase, enabled=enabled, can_trade=can_trade,
                     signal_plan=signal_plan, days_traded=days,
                     near_floor=near_floor, owner=owner)


def lines_for(plan, mid, action=None):
    return [x for x in plan.lines
            if x.mirror_id == mid and (action is None or x.action == action)]


# ------------------------------------------------------------- clone pairing
def test_new_lucid_eval_maps_to_least_loaded_eval_leader():
    e1, e2 = L(1), L(2)
    a = MS.create_mirror("lucid-50k")
    b = MS.create_mirror("lucid-50k")
    b2 = MS.patch_mirror(b.mirror_id, {"leader_id": 1})
    ms = {a.mirror_id: MS.load_mirrors()[a.mirror_id], b2.mirror_id: b2}
    plan = cp.build_plan([e1, e2], ms, TODAY)
    # a has no leader -> mapped to leader 2 (leader 1 already carries b)
    assert plan.desired[a.mirror_id].leader_id == 2
    assert lines_for(plan, a.mirror_id, "MAP")
    # b keeps its existing mapping, no line emitted
    assert plan.desired[b.mirror_id].leader_id == 1
    assert not lines_for(plan, b.mirror_id)


def test_eval_orphan_moves_to_surviving_leader():
    dead, alive = L(1, phase="passed"), L(2)
    m = MS.create_mirror("lucid-50k", leader_id=1)
    ms = MS.load_mirrors()
    plan = cp.build_plan([dead, alive], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id == 2
    mv = lines_for(plan, m.mirror_id, "MOVE")
    assert mv and "leader passed" in mv[0].reason


def test_passed_mirror_unmaps_and_prompts_activation():
    m = MS.create_mirror("tradeify-50k", leader_id=1)
    ms = MS.load_mirrors()
    ms[m.mirror_id].phase = "passed"
    plan = cp.build_plan([L(1)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id is None
    assert lines_for(plan, m.mirror_id, "UNMAP")
    assert lines_for(plan, m.mirror_id, "ACTIVATE")


def test_waiting_mirror_pairs_with_fresh_funded_once():
    fresh = L(9, program="funded", phase="funded", days=0)
    stale = L(8, program="funded", phase="funded", days=3)
    t1 = MS.create_mirror("tradeify-50k")
    t2 = MS.create_mirror("tradeify-50k")
    ms = MS.load_mirrors()
    for m in ms.values():
        m.phase = "waiting"
        m.multiplier = 1.0
    plan = cp.build_plan([fresh, stale], ms, TODAY)
    got = {plan.desired[t1.mirror_id].leader_id, plan.desired[t2.mirror_id].leader_id}
    assert got == {9, None}                     # one twin per fresh leader per firm
    mapped = t1.mirror_id if plan.desired[t1.mirror_id].leader_id == 9 else t2.mirror_id
    assert lines_for(plan, mapped, "MAP")


def test_tradeify_eval_finisher_downsizes_last_day():
    """Near the pass bar, the solver cuts the Tradeify multiplier so the last
    day trades just enough minis (same win probability, smaller red day)."""
    m = MS.create_mirror("tradeify-50k", leader_id=1)
    ms = MS.load_mirrors()
    mm = ms[m.mirror_id]
    # two booked $1,212-net win days: profit 2,424, best 1,212 -> bar ~3,030;
    # remaining ~666 -> 3 minis -> 0.6x (not the standard 0.8x)
    mm.days_traded = 2
    mm.equity = 2_424.0
    mm.eval_best_day = 1_212.0
    plan = cp.build_plan([L(1)], ms, TODAY)
    assert plan.desired[m.mirror_id].multiplier == 0.6
    sm = lines_for(plan, m.mirror_id, "SET_MULT")
    assert sm and "finisher" in sm[0].reason
    # fresh eval keeps the standard scale
    f = MS.create_mirror("tradeify-50k", leader_id=1)
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1)], ms, TODAY)
    assert plan.desired[f.mirror_id].multiplier == 0.8


def test_tradeify_eval_intake_two_per_day_depth_first():
    for _ in range(4):
        MS.create_mirror("tradeify-50k")
    ms = MS.load_mirrors()
    ids = sorted(ms)
    ms[ids[3]].days_traded = 2                        # most advanced -> priority
    ms[ids[1]].leader_id = 1                          # mapped, but loses its slot
    plan = cp.build_plan([L(1), L(2)], ms, TODAY)
    mapped = [k for k in ids if plan.desired[k].leader_id is not None]
    assert set(mapped) == {ids[3], ids[0]}            # 2/day, depth-first
    un = lines_for(plan, ids[1], "UNMAP")
    assert un and "intake slot" in un[0].reason
    assert plan.desired[ids[2]].leader_id is None


def test_tradeify_intake_rides_the_driven_leader():
    # A non-lockstep rider must sit on the leader the depth-first scheduler is
    # actually driving (most advanced), not on an idle fresh one.
    m = MS.create_mirror("tradeify-50k")
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1, days=0), L(2, days=7)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id == 2


def test_lucid_evals_have_no_intake_gate():
    # Lockstep twins inherit the leaders' own 2-slots/day pacing - all of them
    # stay mapped.
    for _ in range(4):
        MS.create_mirror("lucid-50k")
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1), L(2), L(3), L(4)], ms, TODAY)
    assert all(d.leader_id is not None for d in plan.desired.values())


def test_lucid_twins_ride_the_scheduled_drivers():
    # 6 evals, two advanced (they hold tomorrow's 2 depth-first slots): all 4
    # twins must land on those two, split evenly - never on an idle leader
    # (2026-07-13: least-loaded spread parked 3 of 4 twins on idle evals).
    leaders = [L(1), L(2), L(3, days=1), L(4), L(5, days=1), L(6)]
    for _ in range(4):
        MS.create_mirror("lucid-50k")
    plan = cp.build_plan(leaders, MS.load_mirrors(), TODAY)
    got = sorted(d.leader_id for d in plan.desired.values())
    assert got == [3, 3, 5, 5]


def test_lucid_twin_moves_off_alive_but_idle_leader():
    # Sticky pairing must not survive the leader losing its slot: a twin mapped
    # to a live-but-idle eval is MOVEd onto a driver.
    m = MS.create_mirror("lucid-50k", leader_id=1)     # leader 1: alive, idle
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1), L(2, days=3), L(3, days=2)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id == 2
    mv = lines_for(plan, m.mirror_id, "MOVE")
    assert mv and "no eval slot" in mv[0].reason


def test_lucid_twin_keeps_its_driver():
    # A twin already on a slot holder stays put (no churn), even when the other
    # driver is less loaded.
    m = MS.create_mirror("lucid-50k", leader_id=3)
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1), L(2, days=1), L(3, days=1)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id == 3
    assert not lines_for(plan, m.mirror_id)


def test_lucid_drivers_are_per_login():
    # Slot caps are per API key: with 2 slots/login, each login's most-advanced
    # evals drive - a twin can ride login B's slot holder too.
    leaders = [L(1, days=5, owner="A"), L(2, owner="A"), L(3, owner="A"),
               L(4, days=4, owner="B"), L(5, owner="B")]
    for _ in range(4):
        MS.create_mirror("lucid-50k")
    plan = cp.build_plan(leaders, MS.load_mirrors(), TODAY, eval_slots=1)
    got = sorted(d.leader_id for d in plan.desired.values())
    assert got == [1, 1, 4, 4]                        # only the two slot holders


def test_lucid_eval_never_downsizes():
    m = MS.create_mirror("lucid-50k", leader_id=1)
    ms = MS.load_mirrors()
    mm = ms[m.mirror_id]
    mm.days_traded = 1
    mm.equity = 2_900.0        # one leader day from passing
    mm.eval_best_day = 1_530.0
    plan = cp.build_plan([L(1)], ms, TODAY)
    assert plan.desired[m.mirror_id].multiplier == 1.0   # pure clone, lockstep


# ------------------------------------------ near-floor (stopless) leader guard
def test_tradeify_eval_avoids_near_floor_leader():
    # Tradeify (0.8x, non-lockstep, no DLL) must not copy a stopless entry from a
    # leader that is within one stop of its floor - remap to a safe leader.
    m = MS.create_mirror("tradeify-50k", leader_id=1)
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1, near_floor=True), L(2)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id == 2
    mv = lines_for(plan, m.mirror_id, "MOVE")
    assert mv and "near its floor" in mv[0].reason


def test_tradeify_eval_holds_flat_when_all_leaders_near_floor():
    m = MS.create_mirror("tradeify-50k", leader_id=1)
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1, near_floor=True), L(2, near_floor=True)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id is None      # better flat than stopless
    un = lines_for(plan, m.mirror_id, "UNMAP")
    assert un and "near their floor" in un[0].reason


def test_fresh_tradeify_eval_blocked_from_only_near_floor_leader():
    m = MS.create_mirror("tradeify-50k")                    # never mapped
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1, near_floor=True)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id is None


def test_lucid_eval_still_follows_near_floor_leader():
    # Lucid copies 1:1 and dies in lockstep, so a near-floor leader is safe; the
    # guard must NOT strand it.
    m = MS.create_mirror("lucid-50k", leader_id=1)
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1, near_floor=True)], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id == 1
    assert not lines_for(plan, m.mirror_id, "MOVE")


def test_payout_ready_parks_and_queues():
    m = MS.create_mirror("lucid-50k", leader_id=1, phase="funded")
    ms = MS.load_mirrors()
    mm = ms[m.mirror_id]
    mm.payout_ready = True
    mm.equity = mm.peak = 3_000.0
    plan = cp.build_plan([L(1, program="funded", phase="funded")], ms, TODAY)
    assert plan.desired[m.mirror_id].leader_id is None
    assert lines_for(plan, m.mirror_id, "REQUEST_PAYOUT")
    assert plan.payout_queue[0]["amount"] == 1_500.0


# ------------------------------------------------------------- apex fleet
def _apex_setup(n_funded=3, n_eval=3):
    sig = [L(101, signal_plan="apex-nuke", name="PRAC-1"),
           L(102, signal_plan="apex-flip", name="APX-SIG"),
           L(103, signal_plan="apex-eval", name="PRAC-2")]
    for _ in range(n_funded):
        MS.create_mirror("apex-50k", phase="funded")
    for _ in range(n_eval):
        MS.create_mirror("apex-50k")
    ms = MS.load_mirrors()
    for m in ms.values():
        if m.phase == "funded":
            m.channel = "nuke"
    return sig, ms


def test_apex_one_nuke_slot_rotates_and_rest_idle():
    sig, ms = _apex_setup(n_funded=3, n_eval=0)
    funded = sorted([k for k, m in ms.items() if m.phase == "funded"])
    ms[funded[0]].last_nuke_date = "2026-07-01"      # nuked most recently
    plan = cp.build_plan(sig, ms, TODAY)
    holders = [k for k, d in plan.desired.items() if d.nuke_today]
    assert len(holders) == 1
    assert holders[0] == funded[1]                    # longest-since-nuke ("" sorts first)
    assert plan.desired[holders[0]].leader_id == 101  # mapped to the nuke channel
    idle = [k for k in funded if k != holders[0]]
    assert all(plan.desired[k].leader_id is None for k in idle)


def test_apex_nuke_landed_moves_to_flip_channel():
    sig, ms = _apex_setup(n_funded=1, n_eval=0)
    (mid, m), = [(k, v) for k, v in ms.items() if v.phase == "funded"]
    m.last_day_pnl = 1_250.0
    m.last_outcome_date = "2026-07-03"                # nuke won last time it fired
    plan = cp.build_plan(sig, ms, TODAY)
    assert plan.desired[mid].channel == "flip"
    assert plan.desired[mid].leader_id == 102         # the flip channel leader
    assert lines_for(plan, mid, "SET_CHANNEL")


def test_apex_eval_intake_two_per_day_depth_first():
    sig, ms = _apex_setup(n_funded=0, n_eval=4)
    evals = sorted([k for k, m in ms.items() if m.phase == "eval"])
    ms[evals[3]].days_traded = 2                      # most advanced -> priority
    plan = cp.build_plan(sig, ms, TODAY)
    mapped = [k for k in evals if plan.desired[k].leader_id == 103]
    assert len(mapped) == cp.APEX_INTAKE_PER_DAY
    assert evals[3] in mapped                          # depth-first
    waiting = [k for k in evals if plan.desired[k].leader_id is None]
    assert len(waiting) == 2


def test_apex_without_channels_desires_nothing_mapped():
    MS.create_mirror("apex-50k", phase="funded")
    ms = MS.load_mirrors()
    plan = cp.build_plan([L(1)], ms, TODAY)           # no signal leaders at all
    assert all(d.leader_id is None for d in plan.desired.values())


# ------------------------------------------------------------- buy list
def test_buy_list_tops_up_all_pipelines():
    leaders = [L(i) for i in range(1, 4)]             # 3 live topstep evals
    MS.create_mirror("lucid-50k")                     # 1 lucid eval
    for _ in range(2):
        MS.create_mirror("apex-50k", phase="funded")  # 2 PAs, no evals in flight
    ms = MS.load_mirrors()
    plan = cp.build_plan(leaders, ms, TODAY)
    by = {b["firm"]: b for b in plan.buys}
    assert by["Topstep"]["count"] == 3                # 6 - 3 (per login)
    assert by["Lucid"]["count"] == 9                  # 10 - 1
    assert by["Tradeify"]["count"] == 6
    assert by["Apex"]["count"] == 8                   # top-up 8 - 0 (2 PAs < 16)
    assert "pipeline 0/8" in by["Apex"]["reason"]


def test_topstep_pipeline_topped_up_per_login():
    # login A holds 2 evals, login B only a funded account -> each login gets
    # its own top-up row (6 - 2 = 4 and 6 - 0 = 6)
    leaders = [L(1, owner="A"), L(2, owner="A"),
               L(3, program="funded", phase="funded", owner="B")]
    plan = cp.build_plan(leaders, MS.load_mirrors(), TODAY)
    ts = {b["owner"]: b for b in plan.buys if b["firm"] == "Topstep"}
    assert ts["A"]["count"] == 4 and "login A" in ts["A"]["reason"]
    assert ts["B"]["count"] == 6 and "login B" in ts["B"]["reason"]


def test_lucid_buys_throttle_at_three_waiting_passes():
    MS.create_mirror("lucid-50k")                     # 1 eval
    extra = [MS.create_mirror("lucid-50k") for _ in range(3)]
    ms = MS.load_mirrors()
    ms[extra[0].mirror_id].phase = "passed"
    # 1 waiting pass no longer pauses buys (throttle loosened 1 -> 3,
    # 2026-07-11 sweep): standing = 3 evals + 1 passed -> top up 10 - 4 = 6
    plan = cp.build_plan([], ms, TODAY)
    lucid = [b for b in plan.buys if b["firm"] == "Lucid"]
    assert lucid and lucid[0]["count"] == 6
    # ... but 3 waiting passes DO pause buys
    for m in extra:
        ms[m.mirror_id].phase = "passed"
    plan = cp.build_plan([], ms, TODAY)
    assert not any(b["firm"] == "Lucid" for b in plan.buys)


def test_lucid_funded_do_not_consume_eval_slots():
    ms = {}
    for _ in range(4):
        m = MS.create_mirror("lucid-50k")
    ms = MS.load_mirrors()
    ids = sorted(ms)
    for k in ids[:3]:
        ms[k].phase = "funded"                        # 3 funded + 1 eval
    plan = cp.build_plan([], ms, TODAY)
    lucid = [b for b in plan.buys if b["firm"] == "Lucid"]
    # funded accounts freed their slots (eval deleted on funding), so the
    # standing target stays 10 -> need 9 more evals
    assert lucid and lucid[0]["count"] == 9


def test_pipeline_counts_phase_not_cantrade():
    # 2 live evals + 1 auto-blown (below-MLL accounts get phase='blown' from the
    # snapshot, whatever canTrade said) + 1 practice ticket + 1 DLL-locked eval
    # (red day, canTrade=false for the day, still ABOVE the floor -> alive and
    # keeps its slot). Pipeline = 3 (live 2 + DLL-locked), so top-up = 3.
    leaders = [L(1), L(2), L(3, phase="blown", can_trade=True),
               L(4, name="PRAC-X1"), L(5, can_trade=False)]
    leaders[3].practice = True
    plan = cp.build_plan(leaders, MS.load_mirrors(), TODAY)
    ts = [b for b in plan.buys if b["firm"] == "Topstep"]
    assert ts and ts[0]["count"] == 3                 # 6 - 3 holding slots


def test_untradeable_and_practice_accounts_never_lead_mirrors():
    # can_trade=false (force-inactive / DLL-locked today) and practice tickets
    # must not receive NEW mirror mappings — only genuinely live leaders do.
    locked = L(1, can_trade=False)
    prac = L(2, name="PRAC-X1")
    prac.practice = True
    alive = L(3)
    m = MS.create_mirror("lucid-50k", leader_id=1)    # mapped to the locked leader
    ms = MS.load_mirrors()
    plan = cp.build_plan([locked, prac, alive], ms, TODAY)
    # remapped to the only genuinely live eval leader, never the practice ticket
    assert plan.desired[m.mirror_id].leader_id == 3
    assert not locked.live_eval and not prac.live_eval and alive.live_eval
    # ...but the locked one still occupies a pipeline slot (alive, just red today)
    assert locked.pipeline_eval and not prac.pipeline_eval


def test_leaders_from_pool_sees_mll_dead_as_blown():
    """End-to-end: broker says canTrade=true but the balance is under the MLL
    floor -> the snapshot auto-marks the account blown, so the solver's Leader
    arrives phase='blown' (the 'dirty dead account' case that used to inflate
    the pipeline count and pose as a live mirror leader)."""
    from types import SimpleNamespace

    from tophat.broker.base import BrokerAccount
    from tophat.broker.mock import MockBroker

    b = MockBroker(n_eval=2, n_funded=0, seed=77)
    a0 = b.list_accounts()[0]
    # fresh eval floor = 50,000 - 2,000 = 48,000; 47,500 is dead
    b._accounts[0] = BrokerAccount(a0.account_id, a0.name, 47_500.0, True, True)
    pool = [SimpleNamespace(broker=b, mode="mock", owner="deadpool")]
    leaders = {l.account_id: l for l in cp.leaders_from_pool(pool)}
    dead = leaders[a0.account_id]
    assert dead.phase == "blown"
    assert not dead.live_eval and not dead.pipeline_eval
    alive = [l for l in leaders.values() if l.live_eval]
    assert len(alive) == 1


def test_apex_topup_fills_to_target_and_respects_pa_headroom():
    # top-up semantics (2026-07-11 sweep): evals in flight no longer block
    # buys, the pipeline refills to 8
    for _ in range(3):
        MS.create_mirror("apex-50k")                  # 3 evals in flight
    plan = cp.build_plan([], MS.load_mirrors(), TODAY)
    apex = [b for b in plan.buys if b["firm"] == "Apex"]
    assert apex and apex[0]["count"] == 5             # 8 - 3

    for _ in range(8 - 3):
        MS.create_mirror("apex-50k")                  # pipeline full at 8
    plan = cp.build_plan([], MS.load_mirrors(), TODAY)
    assert not any(b["firm"] == "Apex" for b in plan.buys)

    for m in list(MS.load_mirrors()):
        MS.delete_mirror(m)
    for _ in range(16):
        MS.create_mirror("apex-50k", phase="funded")  # 16 PAs >= 16-cap threshold
    plan = cp.build_plan([], MS.load_mirrors(), TODAY)
    assert not any(b["firm"] == "Apex" for b in plan.buys)


# ------------------------------------------------------- determinism / apply
def test_same_state_same_plan():
    sig, ms = _apex_setup()
    p1 = cp.build_plan(sig, ms, TODAY)
    ms2 = MS.load_mirrors()
    for m in ms2.values():
        if m.phase == "funded":
            m.channel = "nuke"
    p2 = cp.build_plan(sig, ms2, TODAY)
    assert cp.plan_to_dict(p1) == cp.plan_to_dict(p2)


def test_apply_then_resolve_yields_no_mapping_lines(tmp_path):
    sig, ms = _apex_setup(n_funded=2, n_eval=2)
    MS.save_mirrors(ms)
    plan = cp.build_plan(sig, ms, TODAY)
    cp.apply_plan(plan, applied_at="2026-07-06 08:00:00 ET", base=tmp_path)
    assert cp.load_plan_dict(TODAY, base=tmp_path)["applied_at"]

    ms2 = MS.load_mirrors()
    plan2 = cp.build_plan(sig, ms2, TODAY)
    mapping_lines = [x for x in plan2.lines
                     if x.action in ("MAP", "MOVE", "UNMAP", "SET_MULT", "SET_CHANNEL")]
    assert mapping_lines == []                        # applied plan re-solves clean
    # nuke-slot holder got its rotation stamp
    holder = [k for k, d in plan.desired.items() if d.nuke_today][0]
    assert ms2[holder].last_nuke_date == TODAY


def test_plan_api_roundtrip(client):
    m = MS.create_mirror("lucid-50k")
    r = client.get("/api/copier-plan")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] and isinstance(body["lines"], list)
    r = client.post("/api/copier-plan/apply")
    assert r.json()["applied_at"]
    r = client.get("/api/copier-plan")
    assert r.json()["applied_at"]                     # sticky for the day
