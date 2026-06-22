"""At-rest encryption: roundtrip, freshness, and tamper detection."""

import base64

import pytest

from tophat.store import crypto


def test_roundtrip_and_not_plaintext():
    token = crypto.encrypt("super-secret-key-123")
    assert token.startswith("v1.")
    assert "super-secret-key-123" not in token
    assert crypto.decrypt(token) == "super-secret-key-123"


def test_fresh_nonce_each_time():
    a, b = crypto.encrypt("same"), crypto.encrypt("same")
    assert a != b                                   # different nonce -> different token
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same"


def test_empty_string_roundtrips():
    assert crypto.decrypt(crypto.encrypt("")) == ""


def test_tamper_is_rejected():
    token = crypto.encrypt("payload")
    parts = token.split(".")
    ct = bytearray(base64.urlsafe_b64decode(parts[2]))
    ct[0] ^= 0x01                                   # flip one bit of ciphertext
    parts[2] = base64.urlsafe_b64encode(bytes(ct)).decode("ascii")
    with pytest.raises(ValueError):
        crypto.decrypt(".".join(parts))


def test_bad_format_is_rejected():
    with pytest.raises(ValueError):
        crypto.decrypt("not-a-token")
