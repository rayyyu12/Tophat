"""Pytest fixtures. Isolates ALL persisted state into a throwaway temp dir so
tests never touch the real data/ files. The env var must be set before any
tophat import (paths.DATA_DIR reads it at import time)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tophat-tests-")
os.environ["TOPHAT_DATA_DIR"] = _TMP
# Point the sim bar cache into the temp dir too, so tests never read the real
# research artifact (tests create their own tiny parquet when they need one).
os.environ["TOPHAT_SIM_BARS"] = os.path.join(_TMP, "sim_bars.parquet")
os.environ.pop("TOPHAT_BROKER", None)  # force mock broker in API tests
os.environ["TOPHAT_SNAPSHOT_TTL"] = "0"  # disable snapshot cache so tests never share stale state
os.environ["TOPHAT_DEBUG_LOG"] = "0"  # don't write debug log files during the test suite
# The suite creates many short-lived app instances serially in one process; the
# real single-instance guard would treat those as a conflict. test_single_instance
# re-enables and exercises the guard directly.
os.environ["TOPHAT_SINGLE_INSTANCE"] = "0"

import pytest

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker

DATA = Path(_TMP)


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh state before every test."""
    import shutil
    for f in DATA.glob("*"):
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)   # e.g. copier_plans/
            else:
                f.unlink()
        except OSError:
            pass
    from tophat.store import simdata
    simdata.reset_cache()   # the parquet above was just deleted
    yield


@pytest.fixture(autouse=True)
def tenant_ctx():
    """Run every test as tenant uid=1 — the id conftest's `client` fixture user
    gets — so direct store calls in test code and API calls through the login
    cookie read/write the SAME per-user files (tophat/store/tenant.py)."""
    from tophat.store import tenant
    token = tenant.set_user(1)
    yield
    tenant.set_user(None)
    try:
        tenant._CURRENT_UID.reset(token)
    except ValueError:
        pass


class ControllableMock(MockBroker):
    """One account whose balance and flat-status the test drives directly."""

    def __init__(self, name="EXPRESS-XFA-1", balance=0.0):
        super().__init__(n_eval=0, n_funded=1, seed=1)
        self.aid = self._accounts[0].account_id
        self.flat = True
        self.set_balance(balance, name)

    def set_balance(self, b, name=None):
        cur = self._accounts[0]
        self._accounts[0] = BrokerAccount(self.aid, name or cur.name, float(b), True, True)

    @property
    def balance(self):
        return self._accounts[0].balance

    def search_open_positions(self, aid):
        return [] if self.flat else [{"contractId": self._nq, "size": 2}]


@pytest.fixture
def ctl():
    return ControllableMock()


@pytest.fixture
def client():
    """Logged-in TestClient against the mock broker."""
    from fastapi.testclient import TestClient
    from tophat.server import auth
    from tophat.server.app import create_app
    auth.create_user("t@x.com", "pw123")
    app = create_app()
    c = TestClient(app)
    with c:
        c.post("/api/login", json={"email": "t@x.com", "password": "pw123"})
        yield c
