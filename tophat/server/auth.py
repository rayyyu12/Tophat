"""Login database + session cookies (stdlib only: sqlite3 + pbkdf2 + hmac).

Single-operator today; the users table is the foundation for the multi-API-key
sharing described in docs/BUILD_PLAN.md §7 (add an api_keys table keyed by user_id).

CLI:
    python -m tophat.server.auth adduser you@example.com 'password'
    python -m tophat.server.auth list
Or seed on startup via env TOPHAT_ADMIN_EMAIL / TOPHAT_ADMIN_PASSWORD.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from tophat.store.paths import AUTH_DB, SECRET_FILE

COOKIE_NAME = "tophat_session"
SESSION_TTL = 7 * 24 * 3600
_PBKDF2_ROUNDS = 200_000


def _db(path: Path = AUTH_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            salt TEXT NOT NULL,
            pwhash TEXT NOT NULL,
            created_at REAL NOT NULL)""")
    conn.commit()
    return conn


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()


def get_secret(path: Path = SECRET_FILE) -> bytes:
    if path.exists():
        return path.read_text().strip().encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    sec = secrets.token_hex(32)
    path.write_text(sec)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return sec.encode()


# --- users ---
def create_user(email: str, password: str, path: Path = AUTH_DB) -> int:
    salt = secrets.token_hex(16)
    with closing(_db(path)) as conn, conn:
        cur = conn.execute(
            "INSERT INTO users(email, salt, pwhash, created_at) VALUES (?,?,?,?)",
            (email.lower().strip(), salt, _hash(password, salt), time.time()))
        return int(cur.lastrowid)


def verify_login(email: str, password: str, path: Path = AUTH_DB) -> int | None:
    with closing(_db(path)) as conn:
        row = conn.execute(
            "SELECT id, salt, pwhash FROM users WHERE email=?",
            (email.lower().strip(),)).fetchone()
    if not row:
        return None
    uid, salt, pwhash = row
    if hmac.compare_digest(_hash(password, salt), pwhash):
        return int(uid)
    return None


def user_count(path: Path = AUTH_DB) -> int:
    with closing(_db(path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def list_users(path: Path = AUTH_DB) -> list[dict]:
    with closing(_db(path)) as conn:
        rows = conn.execute("SELECT id, email, created_at FROM users ORDER BY id").fetchall()
    return [{"id": r[0], "email": r[1], "created_at": r[2]} for r in rows]


# --- session cookies (signed, stateless) ---
def make_cookie(uid: int) -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = f"{uid}.{exp}"
    sig = hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_cookie(value: str | None) -> int | None:
    if not value or value.count(".") != 2:
        return None
    uid, exp, sig = value.split(".")
    expected = hmac.new(get_secret(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if int(exp) < time.time():
        return None
    return int(uid)


def seed_admin_from_env() -> None:
    email = os.getenv("TOPHAT_ADMIN_EMAIL")
    pw = os.getenv("TOPHAT_ADMIN_PASSWORD")
    if email and pw and user_count() == 0:
        create_user(email, pw)


def _cli() -> None:
    import sys
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == "adduser":
        uid = create_user(args[1], args[2])
        print(f"created user #{uid}: {args[1]}")
    elif args and args[0] == "list":
        for u in list_users():
            print(f"#{u['id']}  {u['email']}")
    else:
        print("usage: python -m tophat.server.auth adduser <email> <password> | list")


if __name__ == "__main__":
    _cli()
