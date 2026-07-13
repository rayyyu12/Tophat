"""Per-user (tenant) scoping for the file stores — docs/BUILD_PLAN.md §7.

Every logged-in user gets their own slice of runtime state under
``data/users/<uid>/`` (API keys, registry, lifecycle states, settings, mirrors,
copier plans, sim templates/runs, trade log, schedule). Global, shared files —
the login DB (tophat.db), the session/crypto secret (.secret), the
single-instance lock, and the market-data bar cache — stay at their original
paths and are NOT per-user.

Design: a ContextVar holds the current user id. The server's auth middleware
sets it from the verified session cookie for every request/WS connection; the
automation loop sets it explicitly per user while ticking that user's fleet.
Store functions keep their ``path: Path | None = None`` defaults and call
``resolve(GLOBAL_DEFAULT)``: with a tenant set, the file maps into that user's
directory; with none set (tests, offline research, legacy single-operator CLI
before migration) the original global path is returned unchanged, so existing
behavior is fully preserved.

ContextVar propagation notes (why this is thread-safe here):
- Starlette copies the request context into the threadpool for sync endpoints.
- ``asyncio.to_thread`` copies the caller's context (automation fires).
- Plain ``threading.Thread`` does NOT inherit ContextVars — any new worker
  thread must call ``set_user`` itself (none do today).
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from tophat.store.paths import DATA_DIR

USERS_DIR = DATA_DIR / "users"

# Names (relative to DATA_DIR) that are scoped per user. Anything not listed
# stays global. Directories are listed with a trailing "/" purely for clarity;
# matching is on the first path component.
PER_USER_NAMES = {
    "account_states.json",
    "account_registry.json",
    "settings.json",
    "schedule_state.json",
    "credentials.json",
    "mirrors.json",
    "trade_log.jsonl",
    "sim_templates.json",
    "copier_plans",
    "sim_runs",
    "notify_state.json",
}

_CURRENT_UID: ContextVar[int | None] = ContextVar("tophat_tenant_uid", default=None)


def set_user(uid: int | None):
    """Bind the current context to `uid` (None = legacy/global). Returns the
    ContextVar token so callers that must restore the previous value can."""
    return _CURRENT_UID.set(uid)


def get_user() -> int | None:
    return _CURRENT_UID.get()


@contextmanager
def as_user(uid: int | None):
    token = _CURRENT_UID.set(uid)
    try:
        yield
    finally:
        _CURRENT_UID.reset(token)


def user_dir(uid: int) -> Path:
    return USERS_DIR / str(int(uid))


def resolve(default_path: Path) -> Path:
    """Map a global default store path into the current tenant's directory.

    Only paths directly under DATA_DIR whose first component is in
    PER_USER_NAMES are remapped; everything else (auth DB, .secret, lock,
    bar caches, explicit test paths) passes through untouched.
    """
    uid = _CURRENT_UID.get()
    if uid is None:
        return default_path
    try:
        rel = default_path.relative_to(DATA_DIR)
    except ValueError:
        return default_path
    if not rel.parts or rel.parts[0] not in PER_USER_NAMES:
        return default_path
    out = user_dir(uid) / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def local_operator_uid() -> int:
    """The uid local single-operator tools (CLI/TUI) act as.

    TOPHAT_USER_ID env wins; otherwise the first (oldest) login user; falls
    back to 1 when the auth DB is empty (fresh install — matches the id the
    first created user will get)."""
    env = os.getenv("TOPHAT_USER_ID", "").strip()
    if env:
        return int(env)
    try:
        from tophat.server.auth import list_users
        users = list_users()
        if users:
            return int(users[0]["id"])
    except Exception:
        pass
    return 1


def migrate_legacy_to(uid: int) -> list[str]:
    """One-time move of pre-multi-tenant global files into users/<uid>/.

    Idempotent: only moves a file when it exists at the legacy location AND
    does not already exist in the user dir. Returns the names moved."""
    moved: list[str] = []
    dest_root = user_dir(uid)
    for name in sorted(PER_USER_NAMES):
        src = DATA_DIR / name
        dst = dest_root / name
        if not src.exists() or dst.exists():
            continue
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append(name)
    return moved
