"""Copier plan solver: pairing, apex scheduling, buy list, diff semantics."""

from tophat.services import copier_plan as cp
from tophat.services import mirror_sync
from tophat.store import mirrors as MS

TODAY = "2026-07-06"


def L(aid, program="eval", phase="eval", enabled=True, can_trade=True,
      signal_plan="", days=0, name=None):
    return cp.Leader(account_id=aid, name=name or f"TS-{aid}", program=program,
                     phase=phase, enabled=enabled, can_trade=can_trade,
                     signal_plan=signal_plan, days_traded=days)


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


def test_lucid_eval_never_downsizes():
    m = MS.create_mirror("lucid-50k", leader_id=1)
    ms = MS.load_mirrors()
    mm = ms[m.mirror_id]
    mm.days_traded = 1
    mm.equity = 2_900.0        # one leader day from passing
    mm.eval_best_day = 1_530.0
    plan = cp.build_plan([L(1)], ms, TODAY)
    assert plan.desired[m.mirror_id].multiplier == 1.0   # pure clone, lockstep


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
    assert by["Topstep"]["count"] == 7                # 10 - 3
    assert by["Lucid"]["count"] == 6                  # min(7, 10-0) - 1
    assert by["Tradeify"]["count"] == 10
    assert by["Apex"]["count"] == 10                  # cohort gate open (2 PAs)
    assert "payout-funded" in by["Apex"]["reason"]


def test_lucid_cap_shrinks_with_funded_population():
    ms = {}
    for _ in range(4):
        m = MS.create_mirror("lucid-50k")
    ms = MS.load_mirrors()
    ids = sorted(ms)
    for k in ids[:3]:
        ms[k].phase = "funded"                        # 3 funded + 1 eval
    plan = cp.build_plan([], ms, TODAY)
    lucid = [b for b in plan.buys if b["firm"] == "Lucid"]
    # target = min(7, 10-3) = 7 -> need 6 more evals
    assert lucid and lucid[0]["count"] == 6


def test_apex_cohort_blocked_while_evals_in_flight_or_near_cap():
    MS.create_mirror("apex-50k")                      # eval in flight
    plan = cp.build_plan([], MS.load_mirrors(), TODAY)
    assert not any(b["firm"] == "Apex" for b in plan.buys)

    MS.delete_mirror("apex-01")
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
