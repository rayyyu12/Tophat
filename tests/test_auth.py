"""Unit tests for the login database + session cookies."""

import sqlite3
import time

import pytest

from tophat.server import auth


def test_create_and_verify():
    uid = auth.create_user("a@b.com", "secret")
    assert auth.verify_login("a@b.com", "secret") == uid
    assert auth.verify_login("A@B.COM", "secret") == uid     # case-insensitive
    assert auth.verify_login("a@b.com", "wrong") is None
    assert auth.verify_login("nobody@b.com", "secret") is None


def test_duplicate_email_rejected():
    auth.create_user("dup@b.com", "x")
    with pytest.raises(sqlite3.IntegrityError):
        auth.create_user("dup@b.com", "y")


def test_user_count_and_list():
    assert auth.user_count() == 0
    auth.create_user("one@b.com", "x")
    auth.create_user("two@b.com", "x")
    assert auth.user_count() == 2
    assert {u["email"] for u in auth.list_users()} == {"one@b.com", "two@b.com"}


def test_cookie_roundtrip_and_tamper():
    ck = auth.make_cookie(42)
    assert auth.verify_cookie(ck) == 42
    assert auth.verify_cookie(ck[:-1] + ("0" if ck[-1] != "0" else "1")) is None  # bad sig
    assert auth.verify_cookie("garbage") is None
    assert auth.verify_cookie(None) is None


def test_cookie_expiry():
    # forge an already-expired but correctly-signed cookie
    import hashlib
    import hmac
    exp = int(time.time()) - 10
    payload = f"7.{exp}"
    sig = hmac.new(auth.get_secret(), payload.encode(), hashlib.sha256).hexdigest()
    assert auth.verify_cookie(f"{payload}.{sig}") is None


def test_password_not_stored_plaintext():
    auth.create_user("p@b.com", "plaintextpw")
    from tophat.store.paths import AUTH_DB
    raw = AUTH_DB.read_bytes()
    assert b"plaintextpw" not in raw
