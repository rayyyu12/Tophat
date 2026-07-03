"""run_session live-fire hardening (from the 2026-06-25 debug-log finding):
  - never close an already-flat account (ProjectX errors on it -> aborted the session),
  - one account's broker failure must not stop the rest of the fleet from trading.
"""

from tophat.broker.mock import MockBroker
from tophat.server import service
from tophat.server.service import BrokerHandle
from tophat.store.config import load_settings, save_settings


def _arm(max_evals=2):
    s = load_settings()
    s.auto_execute = True
    s.max_evals_per_day = max_evals
    save_settings(s)


def test_flat_account_is_not_closed_before_entry():
    # hedge_guard (default on) already proves each account flat, so close_contract must
    # NOT be called — on a live ProjectX flat account it returns "error 2" and the
    # unguarded call aborted the entire fire loop (no orders placed for anyone).
    _arm()

    class CloseCounting(MockBroker):
        closes = 0
        def close_contract(self, account_id, contract_id):
            CloseCounting.closes += 1
            return super().close_contract(account_id, contract_id)

    CloseCounting.closes = 0
    b = CloseCounting(n_eval=2, n_funded=1, seed=5)   # all flat to start
    out = service.run_all_sessions([BrokerHandle("o", b, "mock")], execute=True)
    assert out["orders_placed"] >= 1
    assert CloseCounting.closes == 0


def test_one_account_failure_does_not_abort_fleet():
    _arm(max_evals=3)

    class Flaky(MockBroker):
        def place_bracket(self, account_id, contract_id, plan, *, tag=None):
            if account_id == self._accounts[0].account_id:
                raise RuntimeError("simulated broker reject")
            return super().place_bracket(account_id, contract_id, plan, tag=tag)

    b = Flaky(n_eval=3, n_funded=0, seed=6)
    out = service.run_all_sessions([BrokerHandle("o", b, "mock")], execute=True)
    errs = [r for r in out["results"] if r.get("error")]
    assert len(errs) == 1                  # the one bad account is recorded, not fatal
    assert out["orders_placed"] >= 1       # the rest of the fleet still traded
