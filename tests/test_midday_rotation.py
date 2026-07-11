"""Live-fire regressions from the 2026-07-09 first-trades incident:

- A funded account that wins the day's nuke slot and then gets liquidated
  (Topstep flips canTrade=false) must NOT hand its slot to the next funded
  account the same day — one attempt per nuke slot per day.
- The liquidated account's pending trade must still reconcile (BLOWN phase,
  trade-log entry) even though it dropped out of the tradeable set.
- An MLL-dead account must never hold an eval slot (it used to win one and idle
  at the fire loop's floor check, starving live evals).
- Topstep's "enable Auto OCO Brackets" order rejection flags the account in the
  registry and CONSUMES its attempt: one attempt per account per day, no ~30s
  retries (operator rule 2026-07-09 — a later entry is off-strategy).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker
from tophat.server import service
from tophat.server.service import BrokerHandle
from tophat.store import trade_log
from tophat.store.config import load_settings, save_settings
from tophat.store.registry import load_registry
from tophat.store.states import load_all

ET = ZoneInfo("America/New_York")
T0945 = datetime(2026, 7, 9, 9, 45, 0, 200000, tzinfo=ET)
T0949 = datetime(2026, 7, 9, 9, 49, 0, tzinfo=ET)


def _arm(**kw):
    s = load_settings()
    s.auto_execute = True
    for k, v in kw.items():
        setattr(s, k, v)
    save_settings(s)


def _two_funded():
    b = MockBroker(n_eval=0, n_funded=2, seed=1)
    a1, a2 = b._accounts
    b._accounts = [BrokerAccount(a1.account_id, "EXPRESS-V2-1", 0.0, True, True),
                   BrokerAccount(a2.account_id, "EXPRESS-V2-2", 0.0, True, True)]
    return b, a1.account_id, a2.account_id


def test_liquidated_nuke_slot_not_reawarded_same_day():
    _arm()
    b, aid1, aid2 = _two_funded()
    pool = [BrokerHandle("o", b, "mock")]
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0945)
    assert out["orders_placed"] == 1
    fired = next(r["account_id"] for r in out["results"] if r.get("order_id"))
    other = aid2 if fired == aid1 else aid1
    # Topstep liquidates the holder: balance through the floor, canTrade=false, flat.
    b._accounts = [BrokerAccount(a.account_id, a.name,
                                 -2100.0 if a.account_id == fired else a.balance,
                                 a.account_id != fired, True) for a in b._accounts]
    b._positions[fired] = []
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0949)
    assert out["orders_placed"] == 0               # freed slot stays consumed today
    row = next(r for r in out["results"] if r["account_id"] == other)
    assert row["action"] == "idle"


def test_liquidated_account_still_reconciles_and_books_the_loss():
    _arm()
    b, aid1, aid2 = _two_funded()
    pool = [BrokerHandle("o", b, "mock")]
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0945)
    fired = next(r["account_id"] for r in out["results"] if r.get("order_id"))
    b._accounts = [BrokerAccount(a.account_id, a.name,
                                 -2100.0 if a.account_id == fired else a.balance,
                                 a.account_id != fired, True) for a in b._accounts]
    b._positions[fired] = []
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0949)
    assert any(r["account_id"] == fired for r in out["reconciled"])
    st = load_all()[fired]
    assert st.phase.value == "blown"               # floor breached -> terminal
    assert st.pending_label == ""                  # pending resolved, not orphaned
    trades = [e for e in trade_log.read_events() if e.get("type") == "trade"]
    assert any(e["account_id"] == fired and e["outcome"] == "loss" for e in trades)


def test_mll_dead_account_never_holds_an_eval_slot():
    _arm(max_evals_per_day=1)
    b = MockBroker(n_eval=2, n_funded=0, seed=2)
    a1, a2 = b._accounts
    # #1 (lower id, would win the tiebreak) is below its trailing floor.
    b._accounts = [BrokerAccount(a1.account_id, "50KTC-V2-1", 47_500.0, True, True),
                   BrokerAccount(a2.account_id, "50KTC-V2-2", 50_000.0, True, True)]
    out = service.run_all_sessions([BrokerHandle("o", b, "mock")],
                                   execute=True, respect_times=True, now_et=T0945)
    fired = {r["account_id"] for r in out["results"] if r.get("order_id")}
    assert fired == {a2.account_id}                # the live eval got the slot


class OcoRejecting(MockBroker):
    """Rejects brackets like a Topstep account left on Position Brackets."""
    rejecting = True

    def place_bracket(self, account_id, contract_id, plan, *, tag=None):
        if OcoRejecting.rejecting:
            raise RuntimeError("/api/Order/place: error 2: Brackets cannot be "
                               "used with Position Brackets. You must enable "
                               "Auto OCO Brackets.")
        return super().place_bracket(account_id, contract_id, plan, tag=tag)


def test_oco_rejection_consumes_the_attempt_no_retry():
    _arm(max_evals_per_day=1)
    OcoRejecting.rejecting = True
    b = OcoRejecting(n_eval=1, n_funded=0, seed=3)
    aid = b._accounts[0].account_id
    pool = [BrokerHandle("o", b, "mock")]
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0945)
    assert out["orders_placed"] == 0
    assert load_registry().entry(aid).oco_blocked_on == "2026-07-09"
    assert load_all()[aid].last_fire_date == "2026-07-09"   # attempt consumed
    # Even after the operator fixes the platform setting, the account does NOT
    # fire again today — one attempt per day (a 09:49 entry is off-strategy).
    OcoRejecting.rejecting = False
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0949)
    assert out["orders_placed"] == 0
    row = next(r for r in out["results"] if r["account_id"] == aid)
    assert row["action"] == "done"


def test_generic_fire_failure_consumes_the_attempt():
    _arm(max_evals_per_day=1)

    class Flaky(MockBroker):
        fail = True
        def place_bracket(self, account_id, contract_id, plan, *, tag=None):
            if Flaky.fail:
                raise RuntimeError("HTTP 503: gateway hiccup")
            return super().place_bracket(account_id, contract_id, plan, tag=tag)

    Flaky.fail = True
    b = Flaky(n_eval=1, n_funded=0, seed=4)
    aid = b._accounts[0].account_id
    pool = [BrokerHandle("o", b, "mock")]
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0945)
    assert out["orders_placed"] == 0
    assert load_all()[aid].last_fire_date == "2026-07-09"
    Flaky.fail = False
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=T0949)
    assert out["orders_placed"] == 0                # no second attempt


def test_place_timeout_with_live_position_is_treated_as_filled():
    # A place call can raise AFTER the broker accepted it (timeout). If a
    # position exists, the fire really happened: it must record pending (so it
    # reconciles) instead of being written off as a failed attempt.
    _arm(max_evals_per_day=1)

    class AcceptsThenRaises(MockBroker):
        def place_bracket(self, account_id, contract_id, plan, *, tag=None):
            super().place_bracket(account_id, contract_id, plan, tag=tag)
            raise RuntimeError("read timeout waiting for response")

    b = AcceptsThenRaises(n_eval=1, n_funded=0, seed=5)
    aid = b._accounts[0].account_id
    out = service.run_all_sessions([BrokerHandle("o", b, "mock")],
                                   execute=True, respect_times=True, now_et=T0945)
    assert out["orders_placed"] == 0                # not counted as a clean place
    st = load_all()[aid]
    assert st.pending_label == "eval"               # but it WILL reconcile
    assert st.last_fire_date == "2026-07-09"
