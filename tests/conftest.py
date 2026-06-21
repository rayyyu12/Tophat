"""Pytest fixtures. Isolates ALL persisted state into a throwaway temp dir so
tests never touch the real data/ files. The env var must be set before any
tophat import (paths.DATA_DIR reads it at import time)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tophat-tests-")
os.environ["TOPHAT_DATA_DIR"] = _TMP
os.environ.pop("TOPHAT_BROKER", None)  # force mock broker in API tests

import pytest

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker

DATA = Path(_TMP)


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh state before every test."""
    for f in DATA.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    yield


class ControllableMock(MockBroker):
    """One account whose balance and flat-status the test drives directly."""

    def __init__(self, name="XFA-1", balance=0.0):
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
