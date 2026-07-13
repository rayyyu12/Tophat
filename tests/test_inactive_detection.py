"""MLL is the source of truth for blown (operator rule, 2026-07-12): a balance
at/below the trailing floor auto-marks the account BLOWN — persistently, phase
and all — regardless of the broker's canTrade flag. A DLL-only red day (above
the floor) stays active; a pending TopHat trade defers the flip to reconcile."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker
from tophat.engine import Phase
from tophat.server import service
from tophat.store import registry as R
from tophat.store.states import load_all, merge_save

ET = ZoneInfo("America/New_York")
DAY0 = datetime(2026, 6, 22, 9, 46, tzinfo=ET)


def _only_row(broker, key="mll"):
    snap = service.build_snapshot(broker, mode="mock", key=key, owner=key)
    return snap["accounts"][0]


def _with_balance(n_eval, seed, balance):
    b = MockBroker(n_eval=n_eval, n_funded=0, seed=seed)
    acct = b.list_accounts()[0]
    b._accounts[0] = BrokerAccount(acct.account_id, acct.name, balance, True, True)
    return b, acct.account_id


def test_balance_below_mll_auto_marks_blown():
    # Fresh eval: floor = 50,000 - 2,000 = 48,000. Broker still says canTrade=true,
    # but a 47,500 balance is under the MLL -> auto-BLOWN, persisted, no operator step.
    b, aid = _with_balance(1, 61, 47_500.0)
    row = _only_row(b)
    assert row["status"] == "blown"
    assert row["phase"] == "blown"
    assert row["broker_can_trade"] is True          # the flag that lied
    assert row["plan_action"] == "blown"
    assert load_all()[aid].phase == Phase.BLOWN     # persisted, not display-only


def test_dll_day_stays_active():
    # One DLL hit (~$1,050) leaves the balance well above the trailing floor:
    # locked out for the day at most, NOT blown, still holds its pipeline slot.
    b, aid = _with_balance(1, 62, 48_950.0)
    row = _only_row(b, key="mll2")
    assert row["status"] == "active"
    assert row["phase"] == "eval"
    assert row["can_trade"] is True
    assert load_all()[aid].phase == Phase.EVAL


def test_pending_trade_defers_blown_to_reconcile():
    # A TopHat-placed trade awaiting reconciliation must flip the phase via
    # reconcile() (books the loss to the trade log + mirrors first) — the
    # snapshot shows inactive meanwhile, but does NOT pre-empt the flip.
    b, aid = _with_balance(1, 63, 47_500.0)
    service.build_snapshot(b, mode="mock", key="mll3a", owner="mll3a")  # create state
    states = load_all()
    states[aid].phase = Phase.EVAL                  # undo the auto-flip
    states[aid].pending_label = "eval"
    states[aid].pending_entry_balance = 50_000.0
    merge_save(states, [aid])
    service._SNAPSHOT_CACHE.clear()
    row = _only_row(b, key="mll3b")
    assert row["status"] == "inactive"              # dead-looking but deferred
    assert row["phase"] == "eval"
    assert load_all()[aid].phase == Phase.EVAL


def test_run_session_never_fires_below_mll_and_marks_blown(ctl):
    # Funded fresh floor = min(0, peak - 2,000) = -2,000; a -2,500 balance is dead.
    ctl.set_balance(-2_500.0)
    reg = R.load_registry()
    reg.entry(ctl.aid).enabled = True
    R.save_registry(reg)
    r = service.run_session(ctl, execute=True, manual=True, now_et=DAY0)
    row = next(x for x in r["results"] if x["account_id"] == ctl.aid)
    assert row["action"] == "idle"
    assert "MLL" in row["note"]
    assert r["orders_placed"] == 0
    assert load_all()[ctl.aid].phase == Phase.BLOWN  # fire path also persists it


def test_exclude_analytics_roundtrip(client):
    aid = client.get("/api/accounts").json()[0]["account_id"]
    client.post(f"/api/accounts/{aid}/lifecycle", json={"exclude_analytics": True})
    row = next(a for a in client.get("/api/accounts").json()
               if a["account_id"] == aid)
    assert row["exclude_analytics"] is True
