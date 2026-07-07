"""Credential store: encryption at rest, one-key-per-username, masking."""

import pytest

from tophat.store import credentials as C
from tophat.store.paths import CREDENTIALS_FILE


def test_add_and_load():
    C.add_credential("alice", "KEY-ALICE-0001")
    creds = C.load_credentials()
    assert len(creds) == 1
    assert creds[0].username == "alice"
    assert creds[0].api_key == "KEY-ALICE-0001"


def test_key_is_encrypted_at_rest():
    from tophat.store import tenant
    C.add_credential("bob", "PLAINTEXT-SECRET-XYZ")
    raw = tenant.resolve(CREDENTIALS_FILE).read_text(encoding="utf-8")
    assert "PLAINTEXT-SECRET-XYZ" not in raw       # never written in the clear
    assert "api_key_enc" in raw


def test_upsert_one_key_per_username():
    C.add_credential("alice", "KEY-1")
    C.add_credential("ALICE", "KEY-2")             # same user (case-insensitive) -> replace
    creds = C.load_credentials()
    assert len(creds) == 1 and creds[0].api_key == "KEY-2"


def test_delete():
    C.add_credential("alice", "K1")
    C.add_credential("bob", "K2")
    C.delete_credential("alice")
    assert [c.username for c in C.load_credentials()] == ["bob"]


def test_mask_hides_every_char():
    assert C.mask("ABCDEFGH") == "•" * 8


def test_public_list_never_leaks_raw_key():
    C.add_credential("alice", "VERY-SECRET-KEY")
    pub = C.public_list()
    assert "api_key" not in pub[0]
    assert "VERY-SECRET-KEY" not in str(pub)
    assert set(pub[0]["masked"]) <= {"•"}                 # nothing but dots
    assert len(pub[0]["masked"]) == len("VERY-SECRET-KEY")


def test_both_fields_required():
    with pytest.raises(ValueError):
        C.add_credential("", "key")
    with pytest.raises(ValueError):
        C.add_credential("user", "")


def test_seed_from_env_imports_into_store(monkeypatch):
    monkeypatch.setenv("TOPHAT_BROKER", "live")
    monkeypatch.setenv("PROJECTX_USERNAME", "envuser")
    monkeypatch.setenv("PROJECTX_API_KEY", "ENV-KEY-9999")
    C.seed_credentials_from_env()
    creds = C.load_credentials()
    assert [c.username for c in creds] == ["envuser"]
    assert creds[0].api_key == "ENV-KEY-9999"


def test_seed_skips_when_store_already_has_keys(monkeypatch):
    C.add_credential("alice", "K1")
    monkeypatch.setenv("TOPHAT_BROKER", "live")
    monkeypatch.setenv("PROJECTX_USERNAME", "envuser")
    monkeypatch.setenv("PROJECTX_API_KEY", "ENV-KEY")
    C.seed_credentials_from_env()
    assert [c.username for c in C.load_credentials()] == ["alice"]   # not overridden


def test_seed_skips_in_mock_mode(monkeypatch):
    monkeypatch.delenv("TOPHAT_BROKER", raising=False)              # mock default
    monkeypatch.setenv("PROJECTX_USERNAME", "envuser")
    monkeypatch.setenv("PROJECTX_API_KEY", "ENV-KEY")
    C.seed_credentials_from_env()
    assert C.load_credentials() == []
