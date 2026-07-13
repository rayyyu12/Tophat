"""Copier-box pairing tokens (Settings → Copier boxes).

A "box" is one TopHat Rabbit instance on a Tradecopia machine. Pairing mints a
long random bearer token scoped to exactly the two /api/ops/tc-* endpoints and
bound server-side to the minting user's tenant — the token is how a box's pull
resolves to a tenant (plan doc §12.1). Only a SHA-256 of the token is stored;
the plaintext is shown once at creation. Revoke = delete the row.

The registry is deliberately GLOBAL (data/copier_boxes.json, not per-tenant):
an incoming bearer token must resolve to its user before any tenant context
exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.store.atomic import atomic_write_text
from tophat.store.paths import COPIER_BOXES_FILE

_LOCK = threading.Lock()
_ET = ZoneInfo("America/New_York")


def _now() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S ET")


def _seen_ago_s(stamp: str) -> int | None:
    """Seconds since a _now()-shaped stamp; None when blank/unparseable. Lets
    the UI say online/offline without doing ET math in the browser."""
    try:
        then = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S ET").replace(tzinfo=_ET)
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.now(_ET) - then).total_seconds()))


def _load() -> dict:
    if not COPIER_BOXES_FILE.exists():
        return {"boxes": {}}
    try:
        return json.loads(COPIER_BOXES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"boxes": {}}


def _save(data: dict) -> None:
    COPIER_BOXES_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(COPIER_BOXES_FILE, json.dumps(data, indent=2))


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public(box_id: str, b: dict) -> dict:
    return {"box_id": box_id, "name": b.get("name", ""),
            "hint": b.get("hint", ""), "created_at": b.get("created_at", ""),
            "last_seen": b.get("last_seen", ""),
            "seen_ago_s": _seen_ago_s(b.get("last_seen", "")),
            "last_result": b.get("last_result", ""),
            "last_plan_date": b.get("last_plan_date", ""),
            "apply_at": b.get("apply_at", ""),
            "sync_requested": bool(b.get("sync_requested"))}


def create_box(uid: int, name: str) -> tuple[dict, str]:
    """Mint a box + token. Returns (public row, PLAINTEXT token — show once)."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("give the box a name (e.g. 'home desk PC')")
    token = "thr_" + secrets.token_urlsafe(33)   # thr = TopHat Rabbit
    box_id = str(uuid.uuid4())[:8]
    with _LOCK:
        data = _load()
        data["boxes"][box_id] = {
            "uid": int(uid), "name": name, "token_sha256": _sha(token),
            "hint": token[:8] + "…", "created_at": _now(),
            "last_seen": "", "last_result": "", "last_plan_date": "",
        }
        _save(data)
        return _public(box_id, data["boxes"][box_id]), token


def verify(token: str) -> tuple[int, str] | None:
    """token -> (uid, box_id), or None. Constant-time hash comparison."""
    if not token:
        return None
    h = _sha(token)
    data = _load()
    for box_id, b in data["boxes"].items():
        if hmac.compare_digest(h, b.get("token_sha256", "")):
            return int(b["uid"]), box_id
    return None


def touch(box_id: str, *, result: str | None = None,
          plan_date: str | None = None, apply_at: str | None = None) -> None:
    with _LOCK:
        data = _load()
        b = data["boxes"].get(box_id)
        if b is None:
            return
        b["last_seen"] = _now()
        if result is not None:
            b["last_result"] = str(result)
        if plan_date is not None:
            b["last_plan_date"] = str(plan_date)
        if apply_at is not None:
            # the box reports its nightly schedule on every flag poll, so the
            # Operations page can say when Rabbit fires without guessing
            b["apply_at"] = str(apply_at)
        _save(data)


def record_heartbeat(box_id: str, payload: dict) -> None:
    """Store the box's latest Tradecopia health read (Rabbit posts it at
    startup and every ~5 min riding its poll loop — TopHat can't call INTO a
    box behind home NAT, so freshness comes from the box's own cadence)."""
    with _LOCK:
        data = _load()
        b = data["boxes"].get(box_id)
        if b is None:
            return
        b["last_seen"] = _now()
        b["tc"] = dict(payload or {})
        b["tc_at"] = _now()
        _save(data)


def latest_heartbeat(uid: int) -> tuple[dict, int] | None:
    """Newest Tradecopia heartbeat across the user's boxes: (payload,
    age_seconds), or None when no box has ever reported."""
    best: tuple[dict, int] | None = None
    for _bid, b in _load()["boxes"].items():
        if int(b.get("uid", -1)) != int(uid) or not b.get("tc"):
            continue
        age = _seen_ago_s(b.get("tc_at", ""))
        if age is None:
            continue
        if best is None or age < best[1]:
            best = (b["tc"], age)
    return best


def request_sync(uid: int, box_id: str | None = None) -> int:
    """Flag one box (or ALL of the user's boxes) to sync on its next flag poll.
    Set by the 'Sync now' buttons (Settings + Operations) and automatically
    when the operator approves the copier plan. Returns how many boxes were
    flagged."""
    n = 0
    with _LOCK:
        data = _load()
        for bid, b in data["boxes"].items():
            if int(b.get("uid", -1)) != int(uid):
                continue
            if box_id is not None and bid != box_id:
                continue
            b["sync_requested"] = True
            n += 1
        if n:
            _save(data)
    return n


def clear_sync(box_id: str) -> None:
    """The box consumed the request (it pulled the plan or reported a status)."""
    with _LOCK:
        data = _load()
        b = data["boxes"].get(box_id)
        if b and b.get("sync_requested"):
            b["sync_requested"] = False
            _save(data)


def sync_requested(box_id: str) -> bool:
    b = _load()["boxes"].get(box_id)
    return bool(b and b.get("sync_requested"))


def list_boxes(uid: int) -> list[dict]:
    data = _load()
    return sorted((_public(bid, b) for bid, b in data["boxes"].items()
                   if int(b.get("uid", -1)) == int(uid)),
                  key=lambda x: x["created_at"])


def revoke(uid: int, box_id: str) -> bool:
    with _LOCK:
        data = _load()
        b = data["boxes"].get(box_id)
        if b is None or int(b.get("uid", -1)) != int(uid):
            return False
        del data["boxes"][box_id]
        _save(data)
        return True
