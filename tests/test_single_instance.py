"""Single-instance guard + atomic writes (the 2026-06-22 incident fixes)."""

from __future__ import annotations

import json
import os

import pytest

from tophat.store import single_instance
from tophat.store.atomic import atomic_write_text
from tophat.store.single_instance import AlreadyRunning, InstanceLock


def test_second_lock_is_refused(tmp_path):
    lockfile = tmp_path / "tophat.lock"
    first = InstanceLock(lockfile).acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(lockfile).acquire()
    finally:
        first.release()


def test_lock_reacquirable_after_release(tmp_path):
    lockfile = tmp_path / "tophat.lock"
    InstanceLock(lockfile).acquire().release()
    # Once released, a fresh process (here, a fresh handle) can take it again.
    again = InstanceLock(lockfile).acquire()
    again.release()


def test_holder_pid_is_stamped(tmp_path):
    # Read after release: on Windows the held byte-range lock blocks even a
    # separate read handle (precisely why the guard is effective cross-process),
    # so we verify the diagnostic stamp once the lock is freed.
    lockfile = tmp_path / "tophat.lock"
    InstanceLock(lockfile).acquire().release()
    assert f"pid={os.getpid()}" in lockfile.read_text()


def test_env_flag_disables_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPHAT_SINGLE_INSTANCE", "0")
    assert single_instance.acquire_or_none(tmp_path / "tophat.lock") is None


def test_atomic_write_replaces_contents(tmp_path):
    p = tmp_path / "state.json"
    atomic_write_text(p, json.dumps({"a": 1}))
    atomic_write_text(p, json.dumps({"a": 2}))
    assert json.loads(p.read_text()) == {"a": 2}
    # No temp files left behind.
    assert list(tmp_path.glob(".state.json*.tmp")) == []


def test_atomic_write_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deep" / "state.json"
    atomic_write_text(p, "hi")
    assert p.read_text() == "hi"


def test_guard_blocks_second_app_startup(monkeypatch):
    """Integration: with the guard armed, a second app's lifespan must fail."""
    monkeypatch.setenv("TOPHAT_SINGLE_INSTANCE", "1")
    from fastapi.testclient import TestClient

    from tophat.server.app import create_app

    with TestClient(create_app()):           # first instance holds the lock
        with pytest.raises(AlreadyRunning):
            with TestClient(create_app()):   # second instance must be refused
                pass
