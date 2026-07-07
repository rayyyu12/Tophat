"""Per-user (tenant) isolation: stores, legacy migration, and the API surface.

The core promise (docs/BUILD_PLAN.md §7): every login user sees ONLY their own
API keys, accounts, mirrors, and settings. See tophat/store/tenant.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tophat.server import auth
from tophat.server.app import create_app
from tophat.store import credentials as C
from tophat.store import tenant
from tophat.store.config import load_settings, update_settings
from tophat.store.paths import DATA_DIR


# ------------------------------------------------------------- store level
def test_store_isolation_credentials_and_settings():
    with tenant.as_user(1):
        C.add_credential("alice", "KEY-USER1")
        update_settings({"nuke_target_dollars": 3_200.0})
    with tenant.as_user(2):
        assert C.load_credentials() == []            # user 2 sees no keys
        assert load_settings().nuke_target_dollars != 9_999.0
        update_settings({"nuke_target_dollars": 9_999.0})
        C.add_credential("bob", "KEY-USER2")
    with tenant.as_user(1):
        creds = C.load_credentials()
        assert [c.username for c in creds] == ["alice"]
        assert load_settings().nuke_target_dollars == 3_200.0
    assert (tenant.user_dir(1) / "credentials.json").exists()
    assert (tenant.user_dir(2) / "credentials.json").exists()


def test_no_tenant_falls_back_to_legacy_paths():
    with tenant.as_user(None):
        C.add_credential("legacy", "KEY-LEGACY")
        assert (DATA_DIR / "credentials.json").exists()
    with tenant.as_user(7):
        assert C.load_credentials() == []            # tenant never reads legacy


# ------------------------------------------------------------- migration
def test_migrate_legacy_moves_files_once():
    with tenant.as_user(None):
        C.add_credential("op", "KEY-OPERATOR")
        update_settings({"flip_target_dollars": 170.0})
    moved = tenant.migrate_legacy_to(1)
    assert "credentials.json" in moved and "settings.json" in moved
    assert not (DATA_DIR / "credentials.json").exists()
    with tenant.as_user(1):
        assert [c.username for c in C.load_credentials()] == ["op"]
    assert tenant.migrate_legacy_to(1) == []         # idempotent


# ------------------------------------------------------------- API level
def test_api_users_cannot_see_each_others_keys_or_accounts():
    auth.create_user("a@x.com", "pw-a")              # uid 1
    auth.create_user("b@x.com", "pw-b")              # uid 2
    app = create_app()
    with TestClient(app) as c:
        # user A adds a credential and gets accounts
        assert c.post("/api/login", json={"email": "a@x.com",
                                          "password": "pw-a"}).status_code == 200
        c.post("/api/credentials", json={"username": "alice-px",
                                         "api_key": "KEY-A"})
        creds_a = c.get("/api/credentials").json()
        assert [x["username"] for x in creds_a] == ["alice-px"]
        state_a = c.get("/api/state").json()

        # user B logs in on the same server: no keys, not A's accounts
        assert c.post("/api/login", json={"email": "b@x.com",
                                          "password": "pw-b"}).status_code == 200
        assert c.get("/api/credentials").json() == []
        state_b = c.get("/api/state").json()
        owners_b = {g["owner"] for g in state_b["groups"]}
        assert "alice-px" not in owners_b
        # B cannot reveal A's key either
        r = c.post("/api/credentials/alice-px/reveal")
        assert r.status_code == 404

        # back as A: everything still there
        assert c.post("/api/login", json={"email": "a@x.com",
                                          "password": "pw-a"}).status_code == 200
        assert [x["username"] for x in c.get("/api/credentials").json()] == ["alice-px"]
        assert {g["owner"] for g in c.get("/api/state").json()["groups"]} \
            == {g["owner"] for g in state_a["groups"]}


def test_api_settings_are_per_user():
    auth.create_user("a@x.com", "pw-a")
    auth.create_user("b@x.com", "pw-b")
    app = create_app()
    with TestClient(app) as c:
        c.post("/api/login", json={"email": "a@x.com", "password": "pw-a"})
        c.post("/api/settings", json={"nuke_target_dollars": 3_200.0,
                                      "auto_execute": True})
        c.post("/api/login", json={"email": "b@x.com", "password": "pw-b"})
        s_b = c.get("/api/settings").json()
        assert s_b["auto_execute"] is False           # safe default, not A's value
        c.post("/api/settings", json={"nuke_target_dollars": 5_000.0})
        c.post("/api/login", json={"email": "a@x.com", "password": "pw-a"})
        assert c.get("/api/settings").json()["nuke_target_dollars"] == 3_200.0
