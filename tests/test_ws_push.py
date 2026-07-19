"""WS push-on-change: identical snapshots are not re-sent (Render bills egress;
blind 3s re-sends of the full fleet table were ~1 GB/day per open tab)."""

import copy

from tophat.server import auth
from tophat.server.app import ws_comparable


def _snap(balance=50_000.0, as_of="2026-07-18 10:00:00 ET"):
    return {
        "mode": "mock",
        "groups": [{"owner": "t@x.com", "accounts": [
            {"account_id": 1, "balance": balance, "phase": "eval"}]}],
        "accounts": [{"account_id": 1, "balance": balance, "phase": "eval"}],
        "counts": {"total": 1, "funded": 0, "eval": 1},
        "as_of": as_of,
        "as_of_iso": as_of.replace(" ET", ""),
    }


# --- ws_comparable: the change-detection key -----------------------------------

def test_timestamp_only_change_is_not_an_update():
    a = _snap(as_of="2026-07-18 10:00:00 ET")
    b = _snap(as_of="2026-07-18 10:00:03 ET")
    assert ws_comparable(a) == ws_comparable(b)


def test_balance_change_is_an_update():
    assert ws_comparable(_snap(50_000.0)) != ws_comparable(_snap(50_236.5))


def test_new_account_is_an_update():
    a = _snap()
    b = copy.deepcopy(a)
    b["accounts"].append({"account_id": 2, "balance": 50_000.0, "phase": "eval"})
    assert ws_comparable(a) != ws_comparable(b)


def test_key_is_a_serialized_copy_not_a_reference():
    # The cached snapshot's rows can be replaced/mutated by later builds; the
    # key must capture the state at send time so a stale copy never compares
    # equal to genuinely new data.
    snap = _snap()
    key_before = ws_comparable(snap)
    snap["accounts"][0]["balance"] = 99_999.0
    assert ws_comparable(snap) != key_before


# --- /ws still delivers the first frame immediately ----------------------------

def test_ws_sends_initial_snapshot(client):
    cookie = client.cookies.get(auth.COOKIE_NAME)
    with client.websocket_connect(
            "/ws", headers={"cookie": f"{auth.COOKIE_NAME}={cookie}"}) as ws:
        first = ws.receive_json()
    assert isinstance(first.get("accounts"), list) and first["accounts"]
    assert "as_of" in first
